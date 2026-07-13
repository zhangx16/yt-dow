package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"mime"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	backendVersion = "1.0.0"
	maxJSONBody    = 64 << 10
	sessionTTL     = 7 * 24 * time.Hour
)

type server struct {
	ctx          context.Context
	user         string
	password     string
	downloadDir  string
	ytdlpPath    string
	ytdlpVersion string
	queue        chan struct{}

	sessionsMu sync.Mutex
	sessions   map[string]time.Time

	tasksMu sync.RWMutex
	tasks   map[string]*task
	order   []string
	seq     atomic.Uint64
}

type task struct {
	ID       string       `json:"id"`
	Info     taskInfo     `json:"info"`
	Progress taskProgress `json:"progress"`
	Output   taskOutput   `json:"output"`
	Error    string       `json:"error,omitempty"`

	cancel   context.CancelFunc
	canceled bool
}

type taskInfo struct {
	URL            string    `json:"url"`
	Title          string    `json:"title"`
	Thumbnail      string    `json:"thumbnail"`
	Resolution     string    `json:"resolution"`
	FilesizeApprox float64   `json:"filesize_approx"`
	CreatedAt      time.Time `json:"created_at"`
}

type taskProgress struct {
	Percentage    string  `json:"percentage"`
	ProcessStatus int     `json:"process_status"`
	Speed         float64 `json:"speed"`
}

type taskOutput struct {
	SavedFilePath string `json:"savedFilePath"`
}

type rpcRequest struct {
	Method string            `json:"method"`
	Params []json.RawMessage `json:"params"`
	ID     json.RawMessage   `json:"id"`
}

type rpcResponse struct {
	ID     json.RawMessage `json:"id"`
	Result any             `json:"result"`
	Error  any             `json:"error"`
}

type execRequest struct {
	URL    string   `json:"URL"`
	Params []string `json:"Params"`
}

type formatRequest struct {
	URL string `json:"URL"`
}

type fileEntry struct {
	Name        string    `json:"name"`
	Path        string    `json:"path"`
	Size        int64     `json:"size"`
	ModTime     time.Time `json:"modTime"`
	IsVideo     bool      `json:"isVideo"`
	IsDirectory bool      `json:"isDirectory"`
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	downloadDir := envOr("DOWNLOAD_DIR", "/downloads")
	if err := os.MkdirAll(downloadDir, 0o755); err != nil {
		log.Fatalf("create download directory: %v", err)
	}
	absDownloadDir, err := filepath.Abs(downloadDir)
	if err != nil {
		log.Fatalf("resolve download directory: %v", err)
	}

	queueSize := envInt("QUEUE_SIZE", 2)
	if queueSize < 1 {
		queueSize = 1
	}

	s := &server{
		ctx:         ctx,
		user:        envOr("WEBUI_USER", "admin"),
		password:    os.Getenv("WEBUI_PASSWORD"),
		downloadDir: absDownloadDir,
		ytdlpPath:   envOr("YTDLP_PATH", "yt-dlp"),
		queue:       make(chan struct{}, queueSize),
		sessions:    make(map[string]time.Time),
		tasks:       make(map[string]*task),
	}
	if s.password == "" {
		log.Fatal("WEBUI_PASSWORD is required")
	}
	s.ytdlpVersion = commandVersion(ctx, s.ytdlpPath)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /auth/login", s.handleLogin)
	mux.Handle("GET /api/v1/version", s.requireAuth(http.HandlerFunc(s.handleVersion)))
	mux.Handle("POST /rpc/http", s.requireAuth(http.HandlerFunc(s.handleRPC)))
	mux.Handle("POST /filebrowser/downloaded", s.requireAuth(http.HandlerFunc(s.handleFiles)))
	mux.Handle("POST /filebrowser/delete", s.requireAuth(http.HandlerFunc(s.handleFileDelete)))
	mux.Handle("GET /filebrowser/d/", s.requireAuth(http.HandlerFunc(s.handleFileDownload)))
	mux.Handle("HEAD /filebrowser/d/", s.requireAuth(http.HandlerFunc(s.handleFileDownload)))

	httpServer := &http.Server{
		Addr:              ":" + envOr("PORT", "3033"),
		Handler:           securityHeaders(mux),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(shutdownCtx)
	}()

	log.Printf("yt-dlp-lite %s listening on %s", backendVersion, httpServer.Addr)
	if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

func envOr(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envInt(name string, fallback int) int {
	value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(name)))
	if err != nil {
		return fallback
	}
	return value
}

func commandVersion(ctx context.Context, path string) string {
	versionCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(versionCtx, path, "--version").Output()
	if err != nil {
		return "unknown"
	}
	return strings.TrimSpace(string(out))
}

func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		next.ServeHTTP(w, r)
	})
}

func (s *server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = w.Write([]byte("ok"))
}

func (s *server) handleLogin(w http.ResponseWriter, r *http.Request) {
	var credentials struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := decodeJSON(w, r, &credentials); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	userOK := subtle.ConstantTimeCompare([]byte(credentials.Username), []byte(s.user)) == 1
	passwordOK := subtle.ConstantTimeCompare([]byte(credentials.Password), []byte(s.password)) == 1
	if !userOK || !passwordOK {
		http.Error(w, "invalid username or password", http.StatusUnauthorized)
		return
	}

	tokenBytes := make([]byte, 32)
	if _, err := rand.Read(tokenBytes); err != nil {
		http.Error(w, "could not create session", http.StatusInternalServerError)
		return
	}
	token := base64.RawURLEncoding.EncodeToString(tokenBytes)
	s.sessionsMu.Lock()
	s.sessions[token] = time.Now().Add(sessionTTL)
	s.sessionsMu.Unlock()
	writeJSON(w, http.StatusOK, token)
}

func (s *server) requireAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := strings.TrimSpace(r.Header.Get("X-Authentication"))
		if token == "" {
			token = strings.TrimSpace(r.URL.Query().Get("token"))
		}
		if !s.validSession(token) {
			http.Error(w, "authentication required", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (s *server) validSession(token string) bool {
	if token == "" {
		return false
	}
	now := time.Now()
	s.sessionsMu.Lock()
	defer s.sessionsMu.Unlock()
	expires, ok := s.sessions[token]
	if !ok || now.After(expires) {
		delete(s.sessions, token)
		return false
	}
	return true
}

func (s *server) handleVersion(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{
		"rpcVersion":   "lite-" + backendVersion,
		"ytdlpVersion": s.ytdlpVersion,
	})
}

func (s *server) handleRPC(w http.ResponseWriter, r *http.Request) {
	var request rpcRequest
	if err := decodeJSON(w, r, &request); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	response := rpcResponse{ID: request.ID, Error: nil}
	result, err := s.callRPC(r.Context(), request.Method, request.Params)
	if err != nil {
		response.Error = err.Error()
	} else {
		response.Result = result
	}
	writeJSON(w, http.StatusOK, response)
}

func (s *server) callRPC(ctx context.Context, method string, params []json.RawMessage) (any, error) {
	switch method {
	case "Service.Formats":
		var input formatRequest
		if err := firstParam(params, &input); err != nil {
			return nil, err
		}
		if err := validateVideoURL(input.URL); err != nil {
			return nil, err
		}
		return s.videoMetadata(ctx, input.URL)
	case "Service.Exec":
		var input execRequest
		if err := firstParam(params, &input); err != nil {
			return nil, err
		}
		if err := validateVideoURL(input.URL); err != nil {
			return nil, err
		}
		s.enqueue(input)
		return true, nil
	case "Service.Running":
		return s.taskSnapshot(), nil
	case "Service.Kill":
		id, err := stringParam(params)
		if err != nil {
			return nil, err
		}
		return s.removeTask(id, true), nil
	case "Service.Clear":
		id, err := stringParam(params)
		if err != nil {
			return nil, err
		}
		return s.removeTask(id, false), nil
	default:
		return nil, fmt.Errorf("method %q is not available", method)
	}
}

func firstParam(params []json.RawMessage, target any) error {
	if len(params) != 1 {
		return errors.New("exactly one parameter is required")
	}
	if err := json.Unmarshal(params[0], target); err != nil {
		return errors.New("invalid parameter")
	}
	return nil
}

func stringParam(params []json.RawMessage) (string, error) {
	var id string
	if err := firstParam(params, &id); err != nil {
		return "", err
	}
	if id == "" {
		return "", errors.New("task id is required")
	}
	return id, nil
}

func validateVideoURL(value string) error {
	parsed, err := url.ParseRequestURI(strings.TrimSpace(value))
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return errors.New("invalid video URL")
	}
	return nil
}

func (s *server) videoMetadata(parent context.Context, videoURL string) (any, error) {
	ctx, cancel := context.WithTimeout(parent, 90*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, s.ytdlpPath,
		"--dump-single-json",
		"--no-warnings",
		"--no-playlist",
		"--skip-download",
		videoURL,
	)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return nil, errors.New(lastLine(message))
	}
	var metadata any
	if err := json.Unmarshal(out, &metadata); err != nil {
		return nil, errors.New("yt-dlp returned invalid metadata")
	}
	return metadata, nil
}

func (s *server) enqueue(input execRequest) {
	id := strconv.FormatUint(s.seq.Add(1), 10)
	ctx, cancel := context.WithCancel(s.ctx)
	t := &task{
		ID: id,
		Info: taskInfo{
			URL:       input.URL,
			Title:     input.URL,
			CreatedAt: time.Now().UTC(),
		},
		Progress: taskProgress{Percentage: "0%", ProcessStatus: 0},
		cancel:   cancel,
	}
	s.tasksMu.Lock()
	s.tasks[id] = t
	s.order = append([]string{id}, s.order...)
	s.tasksMu.Unlock()
	go s.runQueued(ctx, t, sanitizeParams(input.Params))
}

func sanitizeParams(params []string) []string {
	result := []string{"--no-playlist"}
	for index := 0; index < len(params); index++ {
		switch params[index] {
		case "-f":
			if index+1 < len(params) {
				format := params[index+1]
				index++
				if len(format) <= 1024 && !strings.ContainsAny(format, "\r\n") {
					result = append(result, "-f", format)
				}
			}
		case "--merge-output-format":
			if index+1 < len(params) {
				container := strings.ToLower(params[index+1])
				index++
				if container == "mp4" || container == "mkv" || container == "webm" {
					result = append(result, "--merge-output-format", container)
				}
			}
		}
	}
	return result
}

func (s *server) runQueued(ctx context.Context, t *task, params []string) {
	select {
	case s.queue <- struct{}{}:
		defer func() { <-s.queue }()
	case <-ctx.Done():
		return
	}

	s.updateTask(t.ID, func(current *task) {
		current.Progress.ProcessStatus = 1
		current.Progress.Percentage = "0%"
	})
	err := s.runDownload(ctx, t.ID, t.Info.URL, params)

	s.tasksMu.Lock()
	defer s.tasksMu.Unlock()
	current, exists := s.tasks[t.ID]
	if !exists || current.canceled {
		return
	}
	if err != nil {
		current.Progress.ProcessStatus = 3
		current.Error = err.Error()
		return
	}
	if current.Output.SavedFilePath == "" {
		current.Output.SavedFilePath = s.newestCompletedFile(current.Info.CreatedAt)
	}
	current.Progress.ProcessStatus = 2
	current.Progress.Percentage = "-1"
	current.Progress.Speed = 0
}

func (s *server) runDownload(ctx context.Context, id, videoURL string, params []string) error {
	args := []string{
		"--newline",
		"--no-colors",
		"--progress-template", "download:progress:%(progress._percent_str)s|%(progress.speed)s|%(progress.total_bytes,progress.total_bytes_estimate)s",
		"--print", "before_dl:meta:%(title)s\x1f%(thumbnail)s\x1f%(resolution)s\x1f%(filesize_approx)s",
		"--print", "after_move:filepath:%(filepath)s",
		"-o", filepath.Join(s.downloadDir, "%(title).180B [%(id)s].%(ext)s"),
	}
	args = append(args, params...)

	args = append(args, videoURL)

	cmd := exec.CommandContext(ctx, s.ytdlpPath, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
	cmd.WaitDelay = 3 * time.Second

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return err
	}
	if err := cmd.Start(); err != nil {
		return err
	}

	var scanners sync.WaitGroup
	var errorMu sync.Mutex
	lastError := ""
	scan := func(scanner *bufio.Scanner, captureError bool) {
		defer scanners.Done()
		scanner.Buffer(make([]byte, 64*1024), 1024*1024)
		for scanner.Scan() {
			line := scanner.Text()
			s.handleDownloadLine(id, line)
			if captureError && strings.TrimSpace(line) != "" {
				errorMu.Lock()
				lastError = line
				errorMu.Unlock()
			}
		}
	}
	scanners.Add(2)
	go scan(bufio.NewScanner(stdout), false)
	go scan(bufio.NewScanner(stderr), true)
	scanners.Wait()
	err = cmd.Wait()
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	errorMu.Lock()
	defer errorMu.Unlock()
	if strings.TrimSpace(lastError) != "" {
		return errors.New(lastLine(lastError))
	}
	return err
}

func (s *server) handleDownloadLine(id, line string) {
	switch {
	case strings.HasPrefix(line, "progress:"):
		parts := strings.SplitN(strings.TrimPrefix(line, "progress:"), "|", 3)
		if len(parts) != 3 {
			return
		}
		percentage := strings.TrimSpace(parts[0])
		if percentage == "NA" || percentage == "" {
			percentage = "0%"
		}
		speed, _ := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
		total, _ := strconv.ParseFloat(strings.TrimSpace(parts[2]), 64)
		s.updateTask(id, func(current *task) {
			current.Progress.Percentage = percentage
			current.Progress.Speed = speed
			if total > 0 {
				current.Info.FilesizeApprox = total
			}
		})
	case strings.HasPrefix(line, "meta:"):
		parts := strings.SplitN(strings.TrimPrefix(line, "meta:"), "\x1f", 4)
		s.updateTask(id, func(current *task) {
			if len(parts) > 0 && parts[0] != "NA" && parts[0] != "" {
				current.Info.Title = parts[0]
			}
			if len(parts) > 1 && parts[1] != "NA" {
				current.Info.Thumbnail = parts[1]
			}
			if len(parts) > 2 && parts[2] != "NA" {
				current.Info.Resolution = parts[2]
			}
			if len(parts) > 3 {
				if size, err := strconv.ParseFloat(strings.TrimSpace(parts[3]), 64); err == nil {
					current.Info.FilesizeApprox = size
				}
			}
		})
	case strings.HasPrefix(line, "filepath:"):
		path := strings.TrimSpace(strings.TrimPrefix(line, "filepath:"))
		if s.pathIsContained(path) {
			s.updateTask(id, func(current *task) {
				current.Output.SavedFilePath = path
			})
		}
	}
}

func (s *server) updateTask(id string, update func(*task)) {
	s.tasksMu.Lock()
	defer s.tasksMu.Unlock()
	if current, ok := s.tasks[id]; ok && !current.canceled {
		update(current)
	}
}

func (s *server) removeTask(id string, cancel bool) bool {
	s.tasksMu.Lock()
	defer s.tasksMu.Unlock()
	current, ok := s.tasks[id]
	if !ok {
		return false
	}
	if cancel {
		current.canceled = true
		current.cancel()
	} else if current.Progress.ProcessStatus == 0 || current.Progress.ProcessStatus == 1 {
		return false
	}
	delete(s.tasks, id)
	for index, taskID := range s.order {
		if taskID == id {
			s.order = append(s.order[:index], s.order[index+1:]...)
			break
		}
	}
	return true
}

func (s *server) taskSnapshot() []*task {
	s.tasksMu.RLock()
	defer s.tasksMu.RUnlock()
	result := make([]*task, 0, len(s.tasks))
	for _, id := range s.order {
		current, ok := s.tasks[id]
		if !ok {
			continue
		}
		copy := *current
		copy.cancel = nil
		result = append(result, &copy)
	}
	return result
}

func (s *server) newestCompletedFile(since time.Time) string {
	entries, err := os.ReadDir(s.downloadDir)
	if err != nil {
		return ""
	}
	var newest string
	var newestTime time.Time
	for _, entry := range entries {
		if entry.IsDir() || temporaryFile(entry.Name()) {
			continue
		}
		info, err := entry.Info()
		if err != nil || info.ModTime().Before(since.Add(-5*time.Second)) {
			continue
		}
		if newest == "" || info.ModTime().After(newestTime) {
			newest = filepath.Join(s.downloadDir, entry.Name())
			newestTime = info.ModTime()
		}
	}
	return newest
}

func (s *server) handleFiles(w http.ResponseWriter, r *http.Request) {
	var request struct {
		Subdir string `json:"subdir"`
	}
	if err := decodeJSON(w, r, &request); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if strings.TrimSpace(request.Subdir) != "" {
		http.Error(w, "subdirectories are not available", http.StatusBadRequest)
		return
	}

	entries, err := os.ReadDir(s.downloadDir)
	if err != nil {
		http.Error(w, "could not read downloads", http.StatusInternalServerError)
		return
	}
	files := make([]fileEntry, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() || entry.Type()&os.ModeSymlink != 0 || temporaryFile(entry.Name()) {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			continue
		}
		files = append(files, fileEntry{
			Name:        entry.Name(),
			Path:        filepath.Join(s.downloadDir, entry.Name()),
			Size:        info.Size(),
			ModTime:     info.ModTime().UTC(),
			IsVideo:     videoExtension(entry.Name()),
			IsDirectory: false,
		})
	}
	sort.Slice(files, func(i, j int) bool { return files[i].ModTime.After(files[j].ModTime) })
	writeJSON(w, http.StatusOK, files)
}

func (s *server) handleFileDelete(w http.ResponseWriter, r *http.Request) {
	var request struct {
		Path string `json:"path"`
	}
	if err := decodeJSON(w, r, &request); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	path, err := s.safeFilePath(request.Path)
	if err != nil {
		http.Error(w, "file not found", http.StatusNotFound)
		return
	}
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() {
		http.Error(w, "file not found", http.StatusNotFound)
		return
	}
	if err := os.Remove(path); err != nil {
		http.Error(w, "could not delete file", http.StatusInternalServerError)
		return
	}
	s.removeTasksForFile(path)
	writeJSON(w, http.StatusOK, map[string]bool{"deleted": true})
}

func (s *server) removeTasksForFile(path string) {
	s.tasksMu.Lock()
	defer s.tasksMu.Unlock()
	cleanPath := filepath.Clean(path)
	filtered := s.order[:0]
	for _, id := range s.order {
		current, ok := s.tasks[id]
		if !ok {
			continue
		}
		status := current.Progress.ProcessStatus
		if (status == 2 || status == 3) && filepath.Clean(current.Output.SavedFilePath) == cleanPath {
			delete(s.tasks, id)
			continue
		}
		filtered = append(filtered, id)
	}
	s.order = filtered
}

func temporaryFile(name string) bool {
	lower := strings.ToLower(name)
	return strings.HasSuffix(lower, ".part") || strings.HasSuffix(lower, ".ytdl") || strings.HasSuffix(lower, ".temp")
}

func videoExtension(name string) bool {
	switch strings.ToLower(filepath.Ext(name)) {
	case ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts":
		return true
	default:
		return false
	}
}

func (s *server) handleFileDownload(w http.ResponseWriter, r *http.Request) {
	escaped := strings.TrimPrefix(r.URL.EscapedPath(), "/filebrowser/d/")
	encoded, err := url.PathUnescape(escaped)
	if err != nil {
		http.Error(w, "invalid file path", http.StatusBadRequest)
		return
	}
	decoded, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		http.Error(w, "invalid file path", http.StatusBadRequest)
		return
	}
	path, err := s.safeFilePath(string(decoded))
	if err != nil {
		http.Error(w, "file not found", http.StatusNotFound)
		return
	}
	file, err := os.Open(path)
	if err != nil {
		http.Error(w, "file not found", http.StatusNotFound)
		return
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		http.Error(w, "file not found", http.StatusNotFound)
		return
	}
	disposition := mime.FormatMediaType("attachment", map[string]string{"filename": info.Name()})
	w.Header().Set("Content-Disposition", disposition)
	w.Header().Set("Content-Type", "application/octet-stream")
	http.ServeContent(w, r, info.Name(), info.ModTime(), file)
}

func (s *server) safeFilePath(requested string) (string, error) {
	path := requested
	if !filepath.IsAbs(path) {
		path = filepath.Join(s.downloadDir, path)
	}
	path, err := filepath.Abs(filepath.Clean(path))
	if err != nil || !s.pathIsContained(path) {
		return "", errors.New("path is outside downloads")
	}
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil || !s.pathIsContained(resolved) {
		return "", errors.New("path is outside downloads")
	}
	return resolved, nil
}

func (s *server) pathIsContained(path string) bool {
	absPath, err := filepath.Abs(filepath.Clean(path))
	if err != nil {
		return false
	}
	relative, err := filepath.Rel(s.downloadDir, absPath)
	return err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator))
}

func decodeJSON(w http.ResponseWriter, r *http.Request, target any) error {
	r.Body = http.MaxBytesReader(w, r.Body, maxJSONBody)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return errors.New("invalid JSON request")
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func lastLine(value string) string {
	lines := strings.Split(strings.TrimSpace(value), "\n")
	if len(lines) == 0 {
		return "yt-dlp failed"
	}
	return strings.TrimSpace(lines[len(lines)-1])
}
