// The virtual body the layer presents to the app: a head and two Touch-style
// controllers, all posed in "stage" space (floor-level tracking origin).
//
// Everything the agent asks for is a JSON patch. Patches never take effect
// mid-frame: they queue up and are applied when the app starts its next frame
// (xrWaitFrame returns). That makes every recording frame-exact, so replaying
// it reproduces the session regardless of how fast the controlling process
// happened to be.
#pragma once

#include "common.h"
#include "json.h"

#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <vector>

namespace visor {

enum Hand : int { kLeft = 0, kRight = 1 };

struct Controller
{
	bool connected = true;
	XrPosef aim = pose_identity(); // where it points, -Z along the ray

	float trigger = 0.0f;
	float squeeze = 0.0f;
	float stick_x = 0.0f;
	float stick_y = 0.0f;
	bool stick_click = false;
	bool primary = false;   // X on the left controller, A on the right
	bool secondary = false; // Y on the left controller, B on the right
	bool menu = false;      // left: menu; right: the (system-reserved) Meta button

	// Capacitive sensors. Touch is reported if forced here or implied by use.
	bool touch_trigger = false;
	bool touch_stick = false;
	bool touch_primary = false;
	bool touch_secondary = false;
	bool touch_thumbrest = false;
};

struct RigState
{
	XrPosef head = pose_identity();
	Controller hands[2];
	float ipd = 0.063f;
	// Origin of LOCAL space in stage: where the head was at start (yaw only).
	XrPosef local_origin = pose_identity();
};

struct Velocity
{
	XrVector3f linear{0, 0, 0};
	XrVector3f angular{0, 0, 0};
};

class Rig
{
public:
	Rig();

	// Defaults for a person of the given eye height, standing at the stage
	// centre, facing -Z, controllers held loosely in front.
	void reset(float eye_height);

	// Queue a patch for the next frame boundary. Returns "" or an error.
	std::string queue(const Json& patch);
	// Queue a patch for an absolute frame (used by replay / timelines).
	void schedule(int64_t frame, const Json& patch);
	void clear_schedule();

	// Called once per app frame, right after xrWaitFrame returns.
	void advance_frame(int64_t frame, int64_t frame_time_ns);

	// Recording: every applied patch, stamped with the frame it took effect.
	void start_recording();
	Json stop_recording();
	bool recording() const { return recording_; }

	// Snapshot (thread-safe copy).
	RigState snapshot() const;
	Velocity velocity(int device) const; // 0 head, 1 left, 2 right
	int64_t frame() const;
	size_t pending() const;
	Json to_json() const;

	// Grip pose derived from the aim pose with a Touch-like handle offset.
	static XrPosef grip_from_aim(const XrPosef& aim, int hand);
	static XrPosef aim_from_grip(const XrPosef& grip, int hand);
	static void set_geometry(float grip_pitch_deg, XrVector3f grip_pos);
	static Json geometry_json();

private:
	struct Tween
	{
		int device; // 0 head, 1 left aim, 2 right aim
		XrPosef from, to;
		int64_t start_frame;
		int64_t frames;
		bool ease;
	};

	std::string validate(const Json& patch) const;
	void apply_locked(const Json& patch, int64_t frame);
	void apply_device_locked(int device, const Json& d, int64_t frame, int64_t over, bool ease);
	XrPosef& device_pose_locked(int device);

	mutable std::mutex mu_;
	RigState state_;
	RigState prev_state_;
	int64_t prev_time_ns_ = 0;
	int64_t frame_time_ns_ = 0;
	Velocity vel_[3];
	int64_t frame_ = 0;
	std::deque<Json> pending_;
	std::multimap<int64_t, Json> scheduled_;
	std::vector<Tween> tweens_;
	bool recording_ = false;
	int64_t record_start_ = 0;
	Json record_;
};

// Pose helpers for JSON: {"pos":[x,y,z], "rot":[x,y,z,w]} or "ypr":[y,p,r].
Json pose_to_json(const XrPosef& p);
bool pose_from_json(const Json& j, XrPosef& inout);

} // namespace visor
