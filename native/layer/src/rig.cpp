#include "rig.h"

#include <algorithm>
#include <atomic>

namespace visor {

namespace {

float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

bool vec3_from_json(const Json& j, XrVector3f& out)
{
	if (!j.is_array() || j.size() != 3) {
		return false;
	}
	out = {(float)j.at(0).as_number(), (float)j.at(1).as_number(), (float)j.at(2).as_number()};
	return true;
}

Json vec3_json(XrVector3f v)
{
	Json a = Json::array();
	a.push(v.x);
	a.push(v.y);
	a.push(v.z);
	return a;
}

// Recover yaw/pitch/roll (degrees) from a quaternion, inverse of from_ypr.
XrVector3f to_ypr(XrQuaternionf q)
{
	XrVector3f fwd = rotate(q, {0, 0, -1});
	XrVector3f up = rotate(q, {0, 1, 0});
	float yaw = std::atan2(-fwd.x, -fwd.z);
	float pitch = std::asin(clampf(fwd.y, -1.0f, 1.0f));
	// Roll: angle of 'up' around forward, relative to the no-roll up vector.
	XrQuaternionf no_roll = from_ypr(yaw / kDegToRad, pitch / kDegToRad, 0);
	XrVector3f up0 = rotate(no_roll, {0, 1, 0});
	XrVector3f right0 = rotate(no_roll, {1, 0, 0});
	float roll = std::atan2(-dot(up, right0), dot(up, up0));
	return {yaw / kDegToRad, pitch / kDegToRad, roll / kDegToRad};
}

const char* kDeviceNames[3] = {"head", "left", "right"};

} // namespace

Json pose_to_json(const XrPosef& p)
{
	Json j = Json::object();
	j["pos"] = vec3_json(p.position);
	Json r = Json::array();
	r.push(p.orientation.x);
	r.push(p.orientation.y);
	r.push(p.orientation.z);
	r.push(p.orientation.w);
	j["rot"] = r;
	j["ypr"] = vec3_json(to_ypr(p.orientation));
	return j;
}

bool pose_from_json(const Json& j, XrPosef& inout)
{
	if (!j.is_object()) {
		return false;
	}
	bool any = false;
	if (const Json* pos = j.get("pos")) {
		if (!vec3_from_json(*pos, inout.position)) {
			return false;
		}
		any = true;
	}
	if (const Json* rot = j.get("rot")) {
		if (!rot->is_array() || rot->size() != 4) {
			return false;
		}
		inout.orientation = normalize({(float)rot->at(0).as_number(), (float)rot->at(1).as_number(),
		                               (float)rot->at(2).as_number(), (float)rot->at(3).as_number()});
		any = true;
	} else if (const Json* ypr = j.get("ypr")) {
		XrVector3f a;
		if (!vec3_from_json(*ypr, a)) {
			return false;
		}
		inout.orientation = from_ypr(a.x, a.y, a.z);
		any = true;
	}
	return any;
}

// Controller geometry: where the grip sits relative to the aim ray. Apps tuned
// on a Quest often hang lasers and models off the grip with a fixed rotation,
// so this is what makes them point where the agent aims. 90 degrees (a pistol
// grip, handle nearly upright when pointing level) is what Speakeasy3D's
// Quest-tuned laser lines up with; Meta's legacy-pose constant is 60. Both are
// settable at runtime: {"geometry": {"grip_pitch": .., "grip_pos": [..]}}.
static std::atomic<float> g_grip_pitch_deg{90.0f};
static XrVector3f g_grip_pos{0.0f, -0.03f, 0.04f};
static std::mutex g_geometry_mu;

static XrPosef grip_offset()
{
	std::lock_guard<std::mutex> g(g_geometry_mu);
	return XrPosef{axis_angle({1, 0, 0}, g_grip_pitch_deg.load() * kDegToRad), g_grip_pos};
}

XrPosef Rig::grip_from_aim(const XrPosef& aim, int hand)
{
	(void)hand;
	return compose(aim, grip_offset());
}

XrPosef Rig::aim_from_grip(const XrPosef& grip, int hand)
{
	(void)hand;
	return compose(grip, inverse(grip_offset()));
}

void Rig::set_geometry(float grip_pitch_deg, XrVector3f grip_pos)
{
	std::lock_guard<std::mutex> g(g_geometry_mu);
	g_grip_pitch_deg = grip_pitch_deg;
	g_grip_pos = grip_pos;
}

Json Rig::geometry_json()
{
	std::lock_guard<std::mutex> g(g_geometry_mu);
	Json j = Json::object();
	j["grip_pitch"] = g_grip_pitch_deg.load();
	j["grip_pos"] = vec3_json(g_grip_pos);
	return j;
}

Rig::Rig()
{
	record_ = Json::array();
	reset(1.65f);
}

void Rig::reset(float eye_height)
{
	std::lock_guard<std::mutex> g(mu_);
	state_ = RigState{};
	state_.head = XrPosef{quat_identity(), {0.0f, eye_height, 0.0f}};
	state_.local_origin = state_.head;
	// Hands rest about 40 cm below the eyes, 35 cm out, pointing slightly down.
	XrQuaternionf down = from_ypr(0, -15.0f, 0);
	state_.hands[kLeft].aim = XrPosef{down, {-0.18f, eye_height - 0.40f, -0.35f}};
	state_.hands[kRight].aim = XrPosef{down, {0.18f, eye_height - 0.40f, -0.35f}};
	prev_state_ = state_;
	for (auto& v : vel_) {
		v = Velocity{};
	}
	tweens_.clear();
}

std::string Rig::validate(const Json& patch) const
{
	if (!patch.is_object()) {
		return "patch must be an object";
	}
	for (const auto& kv : patch.entries()) {
		const std::string& k = kv.first;
		if (k == "head" || k == "left" || k == "right") {
			if (!kv.second.is_object()) {
				return k + " must be an object";
			}
			XrPosef scratch = pose_identity();
			if ((kv.second.has("pos") || kv.second.has("rot") || kv.second.has("ypr")) &&
			    !pose_from_json(kv.second, scratch)) {
				return k + ": pos must be [x,y,z], rot [x,y,z,w], ypr [yaw,pitch,roll]";
			}
		} else if (k == "ipd" || k == "over" || k == "ease" || k == "recenter" || k == "geometry") {
			// scalars
		} else {
			return "unknown key '" + k + "' (expected head, left, right, ipd, over, ease)";
		}
	}
	return "";
}

std::string Rig::queue(const Json& patch)
{
	std::string err = validate(patch);
	if (!err.empty()) {
		return err;
	}
	std::lock_guard<std::mutex> g(mu_);
	pending_.push_back(patch);
	return "";
}

void Rig::schedule(int64_t frame, const Json& patch)
{
	if (!validate(patch).empty()) {
		return;
	}
	std::lock_guard<std::mutex> g(mu_);
	scheduled_.emplace(frame, patch);
}

void Rig::clear_schedule()
{
	std::lock_guard<std::mutex> g(mu_);
	scheduled_.clear();
	tweens_.clear();
}

XrPosef& Rig::device_pose_locked(int device)
{
	if (device == 0) {
		return state_.head;
	}
	return state_.hands[device - 1].aim;
}

void Rig::apply_device_locked(int device, const Json& d, int64_t frame, int64_t over, bool ease)
{
	XrPosef& pose = device_pose_locked(device);
	XrPosef target = pose;
	bool posed = false;
	if (device != 0 && d.has("grip")) {
		// Allow posing by grip instead of aim.
		XrPosef grip = grip_from_aim(pose, device - 1);
		if (pose_from_json(*d.get("grip"), grip)) {
			target = aim_from_grip(grip, device - 1);
			posed = true;
		}
	}
	if (d.has("pos") || d.has("rot") || d.has("ypr")) {
		posed = pose_from_json(d, target) || posed;
	}
	if (posed) {
		// Any new pose replaces an in-flight tween for the same device.
		tweens_.erase(std::remove_if(tweens_.begin(), tweens_.end(),
		                             [device](const Tween& t) { return t.device == device; }),
		              tweens_.end());
		if (over > 0) {
			tweens_.push_back(Tween{device, pose, target, frame, over, ease});
		} else {
			pose = target;
		}
	}
	if (device == 0) {
		return;
	}
	Controller& c = state_.hands[device - 1];
	for (const auto& kv : d.entries()) {
		const std::string& k = kv.first;
		const Json& v = kv.second;
		if (k == "trigger") {
			c.trigger = clampf((float)v.as_number(), 0.0f, 1.0f);
		} else if (k == "squeeze" || k == "grip_value") {
			c.squeeze = clampf((float)v.as_number(), 0.0f, 1.0f);
		} else if (k == "stick" || k == "thumbstick") {
			if (v.is_array() && v.size() == 2) {
				c.stick_x = clampf((float)v.at(0).as_number(), -1.0f, 1.0f);
				c.stick_y = clampf((float)v.at(1).as_number(), -1.0f, 1.0f);
			}
		} else if (k == "stick_click" || k == "thumbstick_click") {
			c.stick_click = v.as_bool();
		} else if (k == "a" || k == "x" || k == "primary") {
			c.primary = v.as_bool();
		} else if (k == "b" || k == "y" || k == "secondary") {
			c.secondary = v.as_bool();
		} else if (k == "menu" || k == "system") {
			c.menu = v.as_bool();
		} else if (k == "connected") {
			c.connected = v.as_bool(true);
		} else if (k == "touch" && v.is_object()) {
			for (const auto& t : v.entries()) {
				bool on = t.second.as_bool();
				if (t.first == "trigger") {
					c.touch_trigger = on;
				} else if (t.first == "stick" || t.first == "thumbstick") {
					c.touch_stick = on;
				} else if (t.first == "a" || t.first == "x" || t.first == "primary") {
					c.touch_primary = on;
				} else if (t.first == "b" || t.first == "y" || t.first == "secondary") {
					c.touch_secondary = on;
				} else if (t.first == "thumbrest") {
					c.touch_thumbrest = on;
				}
			}
		}
	}
}

void Rig::apply_locked(const Json& patch, int64_t frame)
{
	int64_t over = patch.has("over") ? patch.get("over")->as_int(0) : 0;
	bool ease = patch.has("ease") ? patch.get("ease")->as_bool(true) : true;
	if (const Json* ipd = patch.get("ipd")) {
		state_.ipd = clampf((float)ipd->as_number(0.063), 0.04f, 0.08f);
	}
	if (const Json* geo = patch.get("geometry")) {
		Json cur = geometry_json();
		float pitch = (float)(geo->get("grip_pitch") ? geo->get("grip_pitch")->as_number() : cur.get("grip_pitch")->as_number());
		XrVector3f pos{};
		if (!geo->get("grip_pos") || !vec3_from_json(*geo->get("grip_pos"), pos)) {
			vec3_from_json(*cur.get("grip_pos"), pos);
		}
		set_geometry(pitch, pos);
	}
	if (patch.has("recenter") && patch.get("recenter")->as_bool()) {
		// LOCAL follows the head, keeping only its yaw (like a long-press recenter).
		XrVector3f ypr = to_ypr(state_.head.orientation);
		state_.local_origin = XrPosef{from_ypr(ypr.x, 0, 0), state_.head.position};
	}
	for (int dev = 0; dev < 3; dev++) {
		if (const Json* d = patch.get(kDeviceNames[dev])) {
			apply_device_locked(dev, *d, frame, over, ease);
		}
	}
	if (recording_) {
		Json e = Json::object();
		e["frame"] = frame - record_start_;
		e["set"] = patch;
		record_.push(e);
	}
}

void Rig::advance_frame(int64_t frame, int64_t frame_time_ns)
{
	std::lock_guard<std::mutex> g(mu_);
	prev_state_ = state_;
	frame_ = frame;

	while (!pending_.empty()) {
		apply_locked(pending_.front(), frame);
		pending_.pop_front();
	}
	while (!scheduled_.empty() && scheduled_.begin()->first <= frame) {
		apply_locked(scheduled_.begin()->second, frame);
		scheduled_.erase(scheduled_.begin());
	}
	for (auto it = tweens_.begin(); it != tweens_.end();) {
		float t = it->frames > 0 ? (float)(frame - it->start_frame) / (float)it->frames : 1.0f;
		t = clampf(t, 0.0f, 1.0f);
		float k = it->ease ? t * t * (3.0f - 2.0f * t) : t;
		device_pose_locked(it->device) = lerp_pose(it->from, it->to, k);
		if (t >= 1.0f) {
			it = tweens_.erase(it);
		} else {
			++it;
		}
	}

	// Velocities from the change over this frame, in stage space.
	int64_t dt_ns = prev_time_ns_ > 0 ? frame_time_ns - prev_time_ns_ : 0;
	prev_time_ns_ = frame_time_ns;
	frame_time_ns_ = frame_time_ns;
	float dt = dt_ns > 0 ? (float)((double)dt_ns * 1e-9) : 0.0f;
	for (int dev = 0; dev < 3; dev++) {
		const XrPosef& a = dev == 0 ? prev_state_.head : prev_state_.hands[dev - 1].aim;
		const XrPosef& b = dev == 0 ? state_.head : state_.hands[dev - 1].aim;
		if (dt <= 0.0f || dt > 0.5f) {
			vel_[dev] = Velocity{};
			continue;
		}
		vel_[dev].linear = (b.position - a.position) * (1.0f / dt);
		// Angular velocity from the delta rotation, as axis * angle / dt.
		XrQuaternionf dq = normalize(b.orientation * conj(a.orientation));
		if (dq.w < 0) {
			dq = {-dq.x, -dq.y, -dq.z, -dq.w};
		}
		float angle = 2.0f * std::acos(clampf(dq.w, -1.0f, 1.0f));
		float s = std::sqrt(std::max(0.0f, 1.0f - dq.w * dq.w));
		XrVector3f axis = s > 1e-6f ? XrVector3f{dq.x / s, dq.y / s, dq.z / s} : XrVector3f{0, 0, 0};
		vel_[dev].angular = axis * (angle / dt);
	}
}

void Rig::start_recording()
{
	std::lock_guard<std::mutex> g(mu_);
	recording_ = true;
	record_start_ = frame_ + 1; // first frame that can still be affected
	record_ = Json::array();
	// Start from a known body: the replay begins by restoring this state.
	Json initial = Json::object();
	initial["head"] = pose_to_json(state_.head);
	for (int h = 0; h < 2; h++) {
		const Controller& c = state_.hands[h];
		Json d = pose_to_json(c.aim);
		d["trigger"] = c.trigger;
		d["squeeze"] = c.squeeze;
		Json st = Json::array();
		st.push(c.stick_x);
		st.push(c.stick_y);
		d["stick"] = st;
		d["stick_click"] = c.stick_click;
		d["primary"] = c.primary;
		d["secondary"] = c.secondary;
		d["menu"] = c.menu;
		d["connected"] = c.connected;
		initial[h == 0 ? "left" : "right"] = d;
	}
	initial["ipd"] = state_.ipd;
	Json e = Json::object();
	e["frame"] = 0;
	e["set"] = initial;
	record_.push(e);
}

Json Rig::stop_recording()
{
	std::lock_guard<std::mutex> g(mu_);
	recording_ = false;
	Json out = Json::object();
	out["frames"] = frame_ - record_start_ + 1;
	// Absolute frame the timeline starts at: with a fixed timestep, replaying
	// at the same absolute frames lines the inputs up with the same game time.
	out["start_frame"] = record_start_;
	out["events"] = record_;
	record_ = Json::array();
	return out;
}

RigState Rig::snapshot() const
{
	std::lock_guard<std::mutex> g(mu_);
	return state_;
}

Velocity Rig::velocity(int device) const
{
	std::lock_guard<std::mutex> g(mu_);
	return vel_[device < 0 || device > 2 ? 0 : device];
}

int64_t Rig::frame() const
{
	std::lock_guard<std::mutex> g(mu_);
	return frame_;
}

size_t Rig::pending() const
{
	std::lock_guard<std::mutex> g(mu_);
	return pending_.size() + scheduled_.size() + tweens_.size();
}

Json Rig::to_json() const
{
	std::lock_guard<std::mutex> g(mu_);
	Json j = Json::object();
	j["frame"] = frame_;
	j["head"] = pose_to_json(state_.head);
	for (int h = 0; h < 2; h++) {
		const Controller& c = state_.hands[h];
		Json d = pose_to_json(c.aim);
		d["grip"] = pose_to_json(grip_from_aim(c.aim, h));
		d["connected"] = c.connected;
		d["trigger"] = c.trigger;
		d["squeeze"] = c.squeeze;
		Json st = Json::array();
		st.push(c.stick_x);
		st.push(c.stick_y);
		d["stick"] = st;
		d["stick_click"] = c.stick_click;
		d[h == kLeft ? "x" : "a"] = c.primary;
		d[h == kLeft ? "y" : "b"] = c.secondary;
		d[h == kLeft ? "menu" : "system"] = c.menu;
		j[h == kLeft ? "left" : "right"] = d;
	}
	j["ipd"] = state_.ipd;
	j["geometry"] = geometry_json();
	j["local_origin"] = pose_to_json(state_.local_origin);
	j["pending"] = (double)(pending_.size() + scheduled_.size() + tweens_.size());
	j["recording"] = recording_;
	return j;
}

} // namespace visor
