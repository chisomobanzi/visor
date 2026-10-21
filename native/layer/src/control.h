// Control socket: how Visor (or anything else) talks to a running app.
//
// A Unix domain socket speaking newline-delimited JSON:
//   -> {"id": 1, "cmd": "set", "args": {...}}
//   <- {"id": 1, "ok": true, "result": {...}}   or  {"id": 1, "ok": false, "error": "..."}
//
// Path: $VISOR_XR_SOCKET if set, else $XDG_RUNTIME_DIR/visor-xr/<pid>.sock
// (falling back to /tmp/visor-xr-<uid>/). Every app running with the layer
// shows up in that directory, which is how `visor sim ls` finds them.
#pragma once

#include "json.h"

#include <atomic>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace visor {

class ControlServer
{
public:
	using Handler = std::function<Json(const std::string& cmd, const Json& args, std::string& error)>;

	explicit ControlServer(Handler handler);
	~ControlServer();

	bool start();
	void stop();
	const std::string& path() const { return path_; }

	static std::string default_dir();

private:
	void accept_loop();
	void client_loop(int fd);

	Handler handler_;
	std::string path_;
	int listen_fd_ = -1;
	int wake_fd_[2] = {-1, -1};
	std::atomic<bool> stop_{false};
	std::thread accept_thread_;
	std::mutex clients_mu_;
	std::condition_variable clients_cv_;
	std::vector<int> client_fds_;
	int active_clients_ = 0;
};

} // namespace visor
