#include "control.h"

#include "common.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <chrono>
#include <cstdlib>
#include <cstring>

namespace visor {

ControlServer::ControlServer(Handler handler) : handler_(std::move(handler)) {}

ControlServer::~ControlServer() { stop(); }

std::string ControlServer::default_dir()
{
	const char* xdg = std::getenv("XDG_RUNTIME_DIR");
	if (xdg && *xdg) {
		return std::string(xdg) + "/visor-xr";
	}
	return "/tmp/visor-xr-" + std::to_string(getuid());
}

bool ControlServer::start()
{
	const char* env = std::getenv("VISOR_XR_SOCKET");
	if (env && *env) {
		path_ = env;
	} else {
		std::string dir = default_dir();
		mkdir(dir.c_str(), 0700);
		path_ = dir + "/" + std::to_string(getpid()) + ".sock";
	}

	sockaddr_un addr{};
	addr.sun_family = AF_UNIX;
	if (path_.size() >= sizeof(addr.sun_path)) {
		LOGE("control socket path too long: %s", path_.c_str());
		return false;
	}
	std::strncpy(addr.sun_path, path_.c_str(), sizeof(addr.sun_path) - 1);

	listen_fd_ = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (listen_fd_ < 0) {
		LOGE("control socket: %s", std::strerror(errno));
		return false;
	}
	unlink(path_.c_str());
	if (bind(listen_fd_, (sockaddr*)&addr, sizeof(addr)) != 0 || listen(listen_fd_, 8) != 0) {
		LOGE("control socket bind %s: %s", path_.c_str(), std::strerror(errno));
		close(listen_fd_);
		listen_fd_ = -1;
		return false;
	}
	chmod(path_.c_str(), 0600);
	if (pipe2(wake_fd_, O_CLOEXEC) != 0) {
		wake_fd_[0] = wake_fd_[1] = -1;
	}
	accept_thread_ = std::thread([this] { accept_loop(); });
	LOGI("control socket at %s", path_.c_str());
	return true;
}

void ControlServer::stop()
{
	if (stop_.exchange(true)) {
		return;
	}
	if (wake_fd_[1] >= 0) {
		char c = 1;
		(void)!write(wake_fd_[1], &c, 1);
	}
	if (accept_thread_.joinable()) {
		accept_thread_.join();
	}
	{
		std::lock_guard<std::mutex> g(clients_mu_);
		for (int fd : client_fds_) {
			shutdown(fd, SHUT_RDWR);
		}
	}
	{
		std::unique_lock<std::mutex> lk(clients_mu_);
		clients_cv_.wait_for(lk, std::chrono::seconds(2), [this] { return active_clients_ == 0; });
	}
	if (listen_fd_ >= 0) {
		close(listen_fd_);
		listen_fd_ = -1;
		unlink(path_.c_str());
	}
	for (int& fd : wake_fd_) {
		if (fd >= 0) {
			close(fd);
			fd = -1;
		}
	}
}

void ControlServer::accept_loop()
{
	while (!stop_) {
		pollfd fds[2] = {{listen_fd_, POLLIN, 0}, {wake_fd_[0], POLLIN, 0}};
		int n = poll(fds, wake_fd_[0] >= 0 ? 2 : 1, 500);
		if (n <= 0 || stop_) {
			continue;
		}
		if (!(fds[0].revents & POLLIN)) {
			continue;
		}
		int fd = accept4(listen_fd_, nullptr, nullptr, SOCK_CLOEXEC);
		if (fd < 0) {
			continue;
		}
		std::lock_guard<std::mutex> g(clients_mu_);
		client_fds_.push_back(fd);
		active_clients_++;
		std::thread([this, fd] { client_loop(fd); }).detach();
	}
}

static bool write_all(int fd, const std::string& s)
{
	size_t off = 0;
	while (off < s.size()) {
		ssize_t w = send(fd, s.data() + off, s.size() - off, MSG_NOSIGNAL);
		if (w < 0) {
			if (errno == EINTR) {
				continue;
			}
			return false;
		}
		off += (size_t)w;
	}
	return true;
}

void ControlServer::client_loop(int fd)
{
	std::string buf;
	char chunk[4096];
	while (!stop_) {
		ssize_t n = recv(fd, chunk, sizeof(chunk), 0);
		if (n <= 0) {
			if (n < 0 && errno == EINTR) {
				continue;
			}
			break;
		}
		buf.append(chunk, (size_t)n);
		size_t nl;
		while ((nl = buf.find('\n')) != std::string::npos) {
			std::string line = buf.substr(0, nl);
			buf.erase(0, nl + 1);
			if (line.empty()) {
				continue;
			}
			std::string perr;
			Json req = Json::parse(line, &perr);
			Json reply = Json::object();
			if (const Json* id = req.get("id")) {
				reply["id"] = *id;
			}
			if (!perr.empty() || !req.is_object()) {
				reply["ok"] = false;
				reply["error"] = "bad request: " + (perr.empty() ? std::string("expected an object") : perr);
			} else {
				std::string cmd = req.get("cmd") ? req.get("cmd")->as_string("") : "";
				const Json* args = req.get("args");
				std::string err;
				Json result = handler_(cmd, args ? *args : Json::object(), err);
				if (err.empty()) {
					reply["ok"] = true;
					reply["result"] = result;
				} else {
					reply["ok"] = false;
					reply["error"] = err;
				}
			}
			if (!write_all(fd, reply.dump() + "\n")) {
				break;
			}
		}
		if (buf.size() > (1 << 20)) {
			break; // nobody sends megabyte commands
		}
	}
	close(fd);
	std::lock_guard<std::mutex> g(clients_mu_);
	for (auto it = client_fds_.begin(); it != client_fds_.end(); ++it) {
		if (*it == fd) {
			client_fds_.erase(it);
			break;
		}
	}
	active_clients_--;
	clients_cv_.notify_all();
}

} // namespace visor
