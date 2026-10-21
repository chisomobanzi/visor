// Visor agent layer — an OpenXR API layer that lets a program drive any
// OpenXR app: head and controller poses, buttons and sticks, with the app's own
// rendered frames coming back out. No engine plugin, no code changes: the app
// is launched with this layer enabled and Visor talks to it over a socket.
//
// What it does, call by call:
//   * xrLocateViews / xrLocateSpace(s): poses come from the virtual rig.
//   * Actions: suggested bindings are recorded; on attach the layer picks the
//     interaction profile it is emulating (Quest Touch by preference) and from
//     then on answers xrGetActionState* itself, from the rig's inputs.
//   * Haptics, session state changes and interaction profile changes are
//     logged as events an agent can read.
//   * Frames: projection-layer swapchain images are copied at release time.
//   * xrWaitFrame can be paused and single-stepped, so an agent can think for
//     as long as it likes without the world moving on.
#include "capture.h"
#include "common.h"
#include "control.h"
#include "json.h"
#include "rig.h"

#include <dlfcn.h>
#include <time.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

namespace visor {

// ---------------------------------------------------------------------------
// Logging

static Level parse_level()
{
	const char* e = std::getenv("VISOR_XR_LOG");
	if (!e) {
		return Level::Info;
	}
	std::string s(e);
	if (s == "error") return Level::Error;
	if (s == "warn") return Level::Warn;
	if (s == "debug") return Level::Debug;
	return Level::Info;
}

Level log_level()
{
	static Level level = parse_level();
	return level;
}

void log(Level level, const char* fmt, ...)
{
	if (level > log_level()) {
		return;
	}
	static const char* names[] = {"error", "warn", "info", "debug"};
	char msg[1024];
	va_list ap;
	va_start(ap, fmt);
	std::vsnprintf(msg, sizeof(msg), fmt, ap);
	va_end(ap);
	std::fprintf(stderr, "[visor-xr] %s: %s\n", names[(int)level], msg);
}

int64_t now_ns()
{
	return std::chrono::duration_cast<std::chrono::nanoseconds>(
	           std::chrono::steady_clock::now().time_since_epoch())
	    .count();
}

static std::string env_str(const char* name, const char* fallback = "")
{
	const char* e = std::getenv(name);
	return e && *e ? std::string(e) : std::string(fallback);
}

static double env_num(const char* name, double fallback)
{
	const char* e = std::getenv(name);
	if (!e || !*e) {
		return fallback;
	}
	char* end = nullptr;
	double v = std::strtod(e, &end);
	return end == e ? fallback : v;
}

// ---------------------------------------------------------------------------
// Next-layer dispatch

#define VISOR_REQUIRED_FUNCS(X)                                                                                 \
	X(DestroyInstance)                                                                                          \
	X(GetInstanceProperties)                                                                                    \
	X(PollEvent)                                                                                                \
	X(GetSystemProperties)                                                                                      \
	X(CreateSession)                                                                                            \
	X(DestroySession)                                                                                           \
	X(BeginSession)                                                                                             \
	X(EndSession)                                                                                               \
	X(RequestExitSession)                                                                                       \
	X(CreateReferenceSpace)                                                                                     \
	X(CreateActionSpace)                                                                                        \
	X(DestroySpace)                                                                                             \
	X(LocateSpace)                                                                                              \
	X(LocateViews)                                                                                              \
	X(CreateSwapchain)                                                                                          \
	X(DestroySwapchain)                                                                                         \
	X(EnumerateSwapchainImages)                                                                                 \
	X(AcquireSwapchainImage)                                                                                    \
	X(ReleaseSwapchainImage)                                                                                    \
	X(WaitFrame)                                                                                                \
	X(BeginFrame)                                                                                               \
	X(EndFrame)                                                                                                 \
	X(StringToPath)                                                                                             \
	X(PathToString)                                                                                             \
	X(CreateActionSet)                                                                                          \
	X(DestroyActionSet)                                                                                         \
	X(CreateAction)                                                                                             \
	X(DestroyAction)                                                                                            \
	X(SuggestInteractionProfileBindings)                                                                        \
	X(AttachSessionActionSets)                                                                                  \
	X(GetCurrentInteractionProfile)                                                                             \
	X(SyncActions)                                                                                              \
	X(GetActionStateBoolean)                                                                                    \
	X(GetActionStateFloat)                                                                                      \
	X(GetActionStateVector2f)                                                                                   \
	X(GetActionStatePose)                                                                                       \
	X(EnumerateBoundSourcesForAction)                                                                           \
	X(GetInputSourceLocalizedName)                                                                              \
	X(ApplyHapticFeedback)                                                                                      \
	X(StopHapticFeedback)

#define VISOR_OPTIONAL_FUNCS(X)                                                                                 \
	X(LocateSpaces)                                                                                             \
	X(LocateSpacesKHR)

struct Dispatch
{
	PFN_xrGetInstanceProcAddr GetInstanceProcAddr = nullptr;
#define DECL(name) PFN_xr##name name = nullptr;
	VISOR_REQUIRED_FUNCS(DECL)
	VISOR_OPTIONAL_FUNCS(DECL)
#undef DECL
};

// ---------------------------------------------------------------------------
// Input model: binding paths -> the rig's canonical controls

enum class Input { None, Trigger, Squeeze, Stick, Primary, Secondary, Menu, Thumbrest, AimPose, GripPose, Haptic };
enum class Comp { Default, Click, Touch, Value, Force, X, Y, Pose };

static const char* input_name(Input i)
{
	switch (i) {
	case Input::Trigger: return "trigger";
	case Input::Squeeze: return "squeeze";
	case Input::Stick: return "stick";
	case Input::Primary: return "primary";
	case Input::Secondary: return "secondary";
	case Input::Menu: return "menu";
	case Input::Thumbrest: return "thumbrest";
	case Input::AimPose: return "aim";
	case Input::GripPose: return "grip";
	case Input::Haptic: return "haptic";
	default: return "none";
	}
}

static const char* comp_name(Comp c)
{
	switch (c) {
	case Comp::Click: return "click";
	case Comp::Touch: return "touch";
	case Comp::Value: return "value";
	case Comp::Force: return "force";
	case Comp::X: return "x";
	case Comp::Y: return "y";
	case Comp::Pose: return "pose";
	default: return "";
	}
}

struct Source
{
	int hand = -1;
	Input input = Input::None;
	Comp comp = Comp::Default;
	std::string path;
};

static bool profile_has_thumbstick(const std::string& profile)
{
	return profile.find("vive_controller") == std::string::npos;
}

// "/user/hand/left/input/trigger/value" in `profile` -> {left, Trigger, Value}
static Source parse_binding(const std::string& profile, const std::string& path)
{
	Source s;
	s.path = path;
	const std::string left = "/user/hand/left/", right = "/user/hand/right/";
	std::string rest;
	if (path.compare(0, left.size(), left) == 0) {
		s.hand = kLeft;
		rest = path.substr(left.size());
	} else if (path.compare(0, right.size(), right) == 0) {
		s.hand = kRight;
		rest = path.substr(right.size());
	} else {
		return s; // head, gamepad, treadmill... not emulated
	}
	std::vector<std::string> parts;
	size_t start = 0;
	while (start <= rest.size()) {
		size_t slash = rest.find('/', start);
		if (slash == std::string::npos) {
			parts.push_back(rest.substr(start));
			break;
		}
		parts.push_back(rest.substr(start, slash - start));
		start = slash + 1;
	}
	if (parts.size() >= 2 && parts[0] == "output" && parts[1] == "haptic") {
		s.input = Input::Haptic;
		return s;
	}
	if (parts.size() < 2 || parts[0] != "input") {
		return s;
	}
	const std::string& id = parts[1];
	std::string comp = parts.size() >= 3 ? parts[2] : "";

	if (id == "trigger" || id == "select" || id == "aim_activate_ext" || id == "pinch_ext") {
		s.input = Input::Trigger;
	} else if (id == "squeeze" || id == "grasp_ext") {
		s.input = Input::Squeeze;
	} else if (id == "thumbstick" || id == "joystick") {
		s.input = Input::Stick;
	} else if (id == "trackpad") {
		// Only controllers without a stick get the stick on their pad.
		s.input = profile_has_thumbstick(profile) ? Input::None : Input::Stick;
	} else if (id == "a" || id == "x") {
		s.input = Input::Primary;
	} else if (id == "b" || id == "y") {
		s.input = Input::Secondary;
	} else if (id == "menu" || id == "system") {
		s.input = Input::Menu;
	} else if (id == "thumbrest") {
		s.input = Input::Thumbrest;
	} else if (id == "aim" || id == "poke_ext") {
		s.input = Input::AimPose;
	} else if (id == "grip" || id == "grip_surface" || id == "palm_ext") {
		s.input = Input::GripPose;
	}
	if ((id == "pinch_ext") && comp == "pose") {
		s.input = Input::AimPose;
	}

	if (comp == "click") s.comp = Comp::Click;
	else if (comp == "touch" || comp == "proximity_fb") s.comp = Comp::Touch;
	else if (comp == "value") s.comp = Comp::Value;
	else if (comp == "force") s.comp = Comp::Force;
	else if (comp == "x") s.comp = Comp::X;
	else if (comp == "y") s.comp = Comp::Y;
	else if (comp == "pose") s.comp = Comp::Pose;
	return s;
}

struct Value
{
	bool b = false;
	float f = 0.0f;
	float x = 0.0f, y = 0.0f;
};

static Value eval_source(const Controller& c, const Source& s, XrActionType type)
{
	float value = 0.0f, x = 0.0f, y = 0.0f;
	bool click = false, touch = false;
	switch (s.input) {
	case Input::Trigger:
		value = c.trigger;
		click = c.trigger >= 0.5f;
		touch = c.touch_trigger || c.trigger > 0.01f;
		break;
	case Input::Squeeze:
		value = c.squeeze;
		click = c.squeeze >= 0.5f;
		touch = c.squeeze > 0.01f;
		break;
	case Input::Stick:
		x = c.stick_x;
		y = c.stick_y;
		click = c.stick_click;
		touch = c.touch_stick || c.stick_click || std::fabs(x) > 0.01f || std::fabs(y) > 0.01f;
		value = std::sqrt(x * x + y * y);
		break;
	case Input::Primary:
		click = c.primary;
		touch = c.touch_primary || c.primary;
		value = click ? 1.0f : 0.0f;
		break;
	case Input::Secondary:
		click = c.secondary;
		touch = c.touch_secondary || c.secondary;
		value = click ? 1.0f : 0.0f;
		break;
	case Input::Menu:
		click = c.menu;
		touch = c.menu;
		value = click ? 1.0f : 0.0f;
		break;
	case Input::Thumbrest:
		touch = c.touch_thumbrest;
		click = touch;
		value = touch ? 1.0f : 0.0f;
		break;
	default: break;
	}

	Value v;
	switch (type) {
	case XR_ACTION_TYPE_BOOLEAN_INPUT:
		switch (s.comp) {
		case Comp::Touch: v.b = touch; break;
		case Comp::Value:
		case Comp::Force: v.b = value >= 0.5f; break;
		case Comp::X: v.b = std::fabs(x) >= 0.5f; break;
		case Comp::Y: v.b = std::fabs(y) >= 0.5f; break;
		default: v.b = click; break;
		}
		break;
	case XR_ACTION_TYPE_FLOAT_INPUT:
		switch (s.comp) {
		case Comp::Click: v.f = click ? 1.0f : 0.0f; break;
		case Comp::Touch: v.f = touch ? 1.0f : 0.0f; break;
		case Comp::X: v.f = x; break;
		case Comp::Y: v.f = y; break;
		default: v.f = s.input == Input::Stick ? y : value; break;
		}
		break;
	case XR_ACTION_TYPE_VECTOR2F_INPUT:
		if (s.input == Input::Stick) {
			v.x = x;
			v.y = y;
		}
		break;
	default: break;
	}
	return v;
}

// Profiles the virtual headset will claim to be, best first. A Quest 3 with
// Touch Plus controllers answers to any of the Touch profiles, then falls
// back to whatever else the app offered bindings for.
static const char* kProfilePreference[] = {
    "/interaction_profiles/meta/touch_controller_plus",
    "/interaction_profiles/oculus/touch_controller",
    "/interaction_profiles/meta/touch_pro_controller",
    "/interaction_profiles/meta/touch_controller_quest_2",
    "/interaction_profiles/meta/touch_controller_quest_1_rift_s",
    "/interaction_profiles/meta/touch_controller_rift_cv1",
    "/interaction_profiles/khr/simple_controller",
    "/interaction_profiles/valve/index_controller",
    "/interaction_profiles/bytedance/pico4_controller",
    "/interaction_profiles/htc/vive_focus3_controller",
    "/interaction_profiles/microsoft/motion_controller",
    "/interaction_profiles/hp/mixed_reality_controller",
    "/interaction_profiles/samsung/odyssey_controller",
    "/interaction_profiles/htc/vive_controller",
    "/interaction_profiles/ext/hand_interaction_ext",
};

// ---------------------------------------------------------------------------
// Layer state

struct ActionSetInfo
{
	std::string name, localized;
	uint32_t priority = 0;
};

struct ActionInfo
{
	XrActionSet set = XR_NULL_HANDLE;
	std::string name, localized;
	XrActionType type = XR_ACTION_TYPE_BOOLEAN_INPUT;
	std::vector<XrPath> subactions;
};

struct Binding
{
	XrAction action;
	Source source;
};

struct InstanceState
{
	XrInstance handle = XR_NULL_HANDLE;
	Dispatch next;
	std::string app_name, engine_name;
	uint32_t app_version = 0, engine_version = 0;
	XrVersion api_version = 0;
	std::vector<std::string> extensions;
	std::string runtime_name;
	XrPath left = XR_NULL_PATH, right = XR_NULL_PATH;
	std::unordered_map<XrActionSet, ActionSetInfo> sets;
	std::unordered_map<XrAction, ActionInfo> actions;
	std::map<std::string, std::vector<std::pair<XrAction, std::string>>> suggested;
	std::unordered_map<XrPath, std::string> path_names;
	PFN_xrConvertTimespecTimeToTimeKHR convert_timespec = nullptr;
	bool time_offset_known = false;
	int64_t time_offset = 0;
};

struct SpaceInfo
{
	XrSession session = XR_NULL_HANDLE;
	enum Kind { Reference, Action } kind = Reference;
	XrReferenceSpaceType ref_type = XR_REFERENCE_SPACE_TYPE_VIEW;
	XrAction action = XR_NULL_HANDLE;
	int hand = -1; // subaction filter, -1 = any
	XrPosef offset = pose_identity();
};

struct ProjectionView
{
	int eye;
	XrSwapchainSubImage sub;
};

struct SwapchainInfo
{
	XrSession session = XR_NULL_HANDLE;
	XrSwapchainCreateInfo info{};
	std::vector<VkImage> images;
	std::deque<uint32_t> acquired;
	std::vector<CaptureView> views; // from the latest xrEndFrame
};

struct Synced
{
	bool active = false;
	bool changed = false;
	XrTime last_change = 0;
	Value v;
};

struct FrameRecord
{
	int64_t frame = 0;
	int64_t wait_start = 0, wait_end = 0, begin = 0, end = 0;
	int64_t display_time = 0, period = 0;
	uint32_t layers = 0;
	bool rendered = false;
	bool gated = false; // held at the pause gate: its gap is our doing, not the app's
};

struct SessionState
{
	XrSession handle = XR_NULL_HANDLE;
	InstanceState* inst = nullptr;
	XrSessionState state = XR_SESSION_STATE_UNKNOWN;
	bool synthetic_unfocused = false;
	bool running = false;

	bool vulkan = false;
	XrGraphicsBindingVulkanKHR vk{};
	std::unique_ptr<FrameSource> capture;

	std::vector<XrActionSet> attached;
	std::string profile;
	std::vector<Binding> bindings;
	std::map<std::pair<XrAction, int>, Synced> synced;
	bool synced_once = false;

	// Frame accounting. waits/begins/ends count calls; a frame's index is the
	// wait index it started with (begins pair with waits in order).
	int64_t waits = 0, begins = 0, ends = 0;
	int64_t render_frame = 0;
	int64_t ended_frame = 0; // index of the last frame passed to xrEndFrame
	XrTime last_display_time = 0;
	XrDuration display_period = 0;
	int64_t wait_start = 0;
	std::deque<FrameRecord> frames;
	std::deque<XrEventDataBuffer> synthetic;
	uint32_t view_count = 2;
	XrFovf last_fov[2] = {};
};

struct Layer
{
	std::mutex mu;
	std::unordered_map<XrInstance, std::unique_ptr<InstanceState>> instances;
	std::unordered_map<XrSession, std::unique_ptr<SessionState>> sessions;
	std::unordered_map<XrSpace, SpaceInfo> spaces;
	std::unordered_map<XrSwapchain, SwapchainInfo> swapchains;
	XrSession current = XR_NULL_HANDLE;

	Rig rig;
	std::atomic<bool> drive{true};
	std::unique_ptr<ControlServer> control;

	// Frame gate. Lock order: gate_mu before mu, never the other way round.
	std::mutex gate_mu;
	std::condition_variable gate_cv;
	bool paused = false;
	int64_t step_budget = 0;
	bool pause_on_focus = false; // VISOR_XR_PAUSED: freeze once the app is up
	int focused_frames = 0;
	std::condition_variable frame_cv; // notified on every xrEndFrame
	bool shutting_down = false;

	// Event log for agents.
	std::mutex ev_mu;
	std::deque<Json> events;
	uint64_t ev_seq = 0;

	// Config read once from the environment.
	float eye_height = 1.65f;
	bool fov_override = false;
	XrFovf fov_left{};
	std::string system_name;
	std::string force_profile;
	bool capture_enabled = true;
	std::string capture_mode = "auto"; // auto | runtime | layer
};

static Layer& L()
{
	static Layer* layer = new Layer(); // never destroyed: apps call us during teardown
	return *layer;
}

static void push_event(Json e)
{
	Layer& l = L();
	std::lock_guard<std::mutex> g(l.ev_mu);
	e["seq"] = (double)++l.ev_seq;
	e["t"] = (double)now_ns();
	l.events.push_back(std::move(e));
	while (l.events.size() > 1000) {
		l.events.pop_front();
	}
}

static const char* session_state_name(XrSessionState s)
{
	switch (s) {
	case XR_SESSION_STATE_IDLE: return "idle";
	case XR_SESSION_STATE_READY: return "ready";
	case XR_SESSION_STATE_SYNCHRONIZED: return "synchronized";
	case XR_SESSION_STATE_VISIBLE: return "visible";
	case XR_SESSION_STATE_FOCUSED: return "focused";
	case XR_SESSION_STATE_STOPPING: return "stopping";
	case XR_SESSION_STATE_LOSS_PENDING: return "loss_pending";
	case XR_SESSION_STATE_EXITING: return "exiting";
	default: return "unknown";
	}
}

// --- helpers that need the next dispatch (call without holding L().mu) ----

static std::string path_string(InstanceState* inst, XrPath path)
{
	if (path == XR_NULL_PATH) {
		return "";
	}
	{
		std::lock_guard<std::mutex> g(L().mu);
		auto it = inst->path_names.find(path);
		if (it != inst->path_names.end()) {
			return it->second;
		}
	}
	char buf[XR_MAX_PATH_LENGTH];
	uint32_t n = 0;
	if (XR_FAILED(inst->next.PathToString(inst->handle, path, sizeof(buf), &n, buf))) {
		return "";
	}
	std::string s(buf);
	std::lock_guard<std::mutex> g(L().mu);
	inst->path_names[path] = s;
	return s;
}

static XrPath string_path(InstanceState* inst, const std::string& s)
{
	XrPath p = XR_NULL_PATH;
	inst->next.StringToPath(inst->handle, s.c_str(), &p);
	return p;
}

static int hand_of(InstanceState* inst, XrPath subaction)
{
	if (subaction == XR_NULL_PATH) {
		return -1;
	}
	if (subaction == inst->left) {
		return kLeft;
	}
	if (subaction == inst->right) {
		return kRight;
	}
	return -2; // some other top-level path
}

// --- pose of any space we know, in stage space (call with L().mu held) -----

static bool focused_locked(const SessionState& s)
{
	return s.state == XR_SESSION_STATE_FOCUSED && !s.synthetic_unfocused;
}

static bool space_pose_locked(const SpaceInfo& sp, const RigState& rig, XrPosef* out, bool* tracked)
{
	*tracked = true;
	XrPosef base = pose_identity();
	if (sp.kind == SpaceInfo::Reference) {
		switch (sp.ref_type) {
		case XR_REFERENCE_SPACE_TYPE_VIEW: base = rig.head; break;
		case XR_REFERENCE_SPACE_TYPE_LOCAL: base = rig.local_origin; break;
		case XR_REFERENCE_SPACE_TYPE_LOCAL_FLOOR:
			base = rig.local_origin;
			base.position.y = 0.0f;
			break;
		case XR_REFERENCE_SPACE_TYPE_STAGE:
		case XR_REFERENCE_SPACE_TYPE_UNBOUNDED_MSFT: base = pose_identity(); break;
		default: return false;
		}
		*out = compose(base, sp.offset);
		return true;
	}
	// Action space: find the pose binding for its hand(s).
	auto sit = L().sessions.find(sp.session);
	if (sit == L().sessions.end()) {
		return false;
	}
	const SessionState& s = *sit->second;
	for (const auto& b : s.bindings) {
		if (b.action != sp.action) {
			continue;
		}
		if (sp.hand >= 0 && b.source.hand != sp.hand) {
			continue;
		}
		if (b.source.hand < 0 || (b.source.input != Input::AimPose && b.source.input != Input::GripPose)) {
			continue;
		}
		const Controller& c = rig.hands[b.source.hand];
		if (!c.connected) {
			*tracked = false;
			return false;
		}
		base = b.source.input == Input::AimPose ? c.aim : Rig::grip_from_aim(c.aim, b.source.hand);
		*out = compose(base, sp.offset);
		return true;
	}
	*tracked = false;
	return false;
}

static int space_device(const SpaceInfo& sp)
{
	if (sp.kind == SpaceInfo::Reference) {
		return sp.ref_type == XR_REFERENCE_SPACE_TYPE_VIEW ? 0 : -1;
	}
	auto sit = L().sessions.find(sp.session);
	if (sit == L().sessions.end()) {
		return -1;
	}
	for (const auto& b : sit->second->bindings) {
		if (b.action == sp.action && (sp.hand < 0 || b.source.hand == sp.hand) && b.source.hand >= 0) {
			return 1 + b.source.hand;
		}
	}
	return -1;
}

// Fill location (and chained velocity) for space relative to base.
// Returns false when either space isn't one we model.
static bool locate_locked(XrSpace space, XrSpace base_space, XrSpaceLocationFlags* flags, XrPosef* pose,
                          XrSpaceVelocityFlags* vflags, XrVector3f* lin, XrVector3f* ang)
{
	Layer& l = L();
	auto a = l.spaces.find(space);
	auto b = l.spaces.find(base_space);
	if (a == l.spaces.end() || b == l.spaces.end()) {
		return false;
	}
	RigState rig = l.rig.snapshot();
	XrPosef wa, wb;
	bool ta = true, tb = true;
	bool va = space_pose_locked(a->second, rig, &wa, &ta);
	bool vb = space_pose_locked(b->second, rig, &wb, &tb);
	if (!va && a->second.kind == SpaceInfo::Reference) {
		return false; // reference type we don't model
	}
	if (!vb && b->second.kind == SpaceInfo::Reference) {
		return false;
	}
	if (va && vb) {
		*pose = relative(wb, wa);
		*flags = XR_SPACE_LOCATION_ORIENTATION_VALID_BIT | XR_SPACE_LOCATION_POSITION_VALID_BIT |
		         XR_SPACE_LOCATION_ORIENTATION_TRACKED_BIT | XR_SPACE_LOCATION_POSITION_TRACKED_BIT;
	} else {
		*flags = 0;
	}
	if (vflags) {
		*vflags = 0;
		*lin = XrVector3f{0, 0, 0};
		*ang = XrVector3f{0, 0, 0};
		if (va && vb) {
			int da = space_device(a->second), db = space_device(b->second);
			Velocity v{};
			if (da >= 0) {
				v = l.rig.velocity(da);
			}
			if (db >= 0) {
				Velocity vb2 = l.rig.velocity(db);
				v.linear = v.linear - vb2.linear;
				v.angular = v.angular - vb2.angular;
			}
			XrQuaternionf inv = conj(wb.orientation);
			*lin = rotate(inv, v.linear);
			*ang = rotate(inv, v.angular);
			*vflags = XR_SPACE_VELOCITY_LINEAR_VALID_BIT | XR_SPACE_VELOCITY_ANGULAR_VALID_BIT;
		}
	}
	return true;
}

// --- action state ----------------------------------------------------------

static void choose_profile_locked(SessionState& s)
{
	InstanceState* inst = s.inst;
	std::set<XrAction> attached_actions;
	for (const auto& kv : inst->actions) {
		if (std::find(s.attached.begin(), s.attached.end(), kv.second.set) != s.attached.end()) {
			attached_actions.insert(kv.first);
		}
	}
	std::string chosen;
	if (!L().force_profile.empty() && inst->suggested.count(L().force_profile)) {
		chosen = L().force_profile;
	}
	for (const char* p : kProfilePreference) {
		if (!chosen.empty()) {
			break;
		}
		if (inst->suggested.count(p)) {
			chosen = p;
		}
	}
	if (chosen.empty() && !inst->suggested.empty()) {
		chosen = inst->suggested.begin()->first;
	}
	s.profile = chosen;
	s.bindings.clear();
	if (chosen.empty()) {
		return;
	}
	for (const auto& ab : inst->suggested[chosen]) {
		if (!attached_actions.count(ab.first)) {
			continue;
		}
		Source src = parse_binding(chosen, ab.second);
		if (src.hand < 0 || src.input == Input::None) {
			continue;
		}
		s.bindings.push_back(Binding{ab.first, src});
	}
}

static Value combine(XrActionType type, const Value& acc, const Value& v, bool first)
{
	if (first) {
		return v;
	}
	Value out = acc;
	switch (type) {
	case XR_ACTION_TYPE_BOOLEAN_INPUT: out.b = acc.b || v.b; break;
	case XR_ACTION_TYPE_FLOAT_INPUT: out.f = std::fabs(v.f) > std::fabs(acc.f) ? v.f : acc.f; break;
	case XR_ACTION_TYPE_VECTOR2F_INPUT:
		if (v.x * v.x + v.y * v.y > acc.x * acc.x + acc.y * acc.y) {
			out.x = v.x;
			out.y = v.y;
		}
		break;
	default: break;
	}
	return out;
}

static bool value_equal(XrActionType type, const Value& a, const Value& b)
{
	switch (type) {
	case XR_ACTION_TYPE_BOOLEAN_INPUT: return a.b == b.b;
	case XR_ACTION_TYPE_FLOAT_INPUT: return a.f == b.f;
	case XR_ACTION_TYPE_VECTOR2F_INPUT: return a.x == b.x && a.y == b.y;
	default: return true;
	}
}

// Recompute every attached action's state, as xrSyncActions would.
static void sync_locked(SessionState& s, const XrActionsSyncInfo* info, const RigState& rig)
{
	InstanceState* inst = s.inst;
	// Which hands are active per action set.
	std::map<XrActionSet, std::pair<bool, bool>> active; // left, right
	for (uint32_t i = 0; i < info->countActiveActionSets; i++) {
		const XrActiveActionSet& a = info->activeActionSets[i];
		int h = hand_of(inst, a.subactionPath);
		auto& lr = active[a.actionSet];
		if (h == -1) {
			lr = {true, true};
		} else if (h == kLeft) {
			lr.first = true;
		} else if (h == kRight) {
			lr.second = true;
		}
	}
	bool focused = focused_locked(s);
	XrTime now = s.last_display_time;

	for (const auto& kv : inst->actions) {
		XrAction action = kv.first;
		const ActionInfo& ai = kv.second;
		if (std::find(s.attached.begin(), s.attached.end(), ai.set) == s.attached.end()) {
			continue;
		}
		if (ai.type == XR_ACTION_TYPE_VIBRATION_OUTPUT) {
			continue;
		}
		auto set_it = active.find(ai.set);
		std::vector<int> keys = {-1};
		for (XrPath sp : ai.subactions) {
			int h = hand_of(inst, sp);
			if (h >= 0) {
				keys.push_back(h);
			}
		}
		for (int key : keys) {
			bool hands_on[2] = {false, false};
			if (set_it != active.end()) {
				hands_on[kLeft] = set_it->second.first && (key == -1 || key == kLeft);
				hands_on[kRight] = set_it->second.second && (key == -1 || key == kRight);
			}
			Value acc;
			bool any = false;
			if (focused) {
				for (const auto& b : s.bindings) {
					if (b.action != action || !hands_on[b.source.hand]) {
						continue;
					}
					const Controller& c = rig.hands[b.source.hand];
					if (!c.connected) {
						continue;
					}
					if (ai.type == XR_ACTION_TYPE_POSE_INPUT) {
						any = true;
						continue;
					}
					Value v = eval_source(c, b.source, ai.type);
					acc = combine(ai.type, acc, v, !any);
					any = true;
				}
			}
			Synced& st = s.synced[{action, key}];
			Synced next;
			next.active = any;
			next.v = any ? acc : Value{};
			next.changed = any && st.active && !value_equal(ai.type, st.v, next.v);
			// A press that starts in the same sync the action becomes active
			// still counts as a change from the default.
			if (any && !st.active && !value_equal(ai.type, Value{}, next.v)) {
				next.changed = true;
			}
			next.last_change = next.changed ? now : st.last_change;
			st = next;
		}
	}
	s.synced_once = true;
}

static SessionState* session_locked(XrSession session)
{
	auto it = L().sessions.find(session);
	return it == L().sessions.end() ? nullptr : it->second.get();
}

static InstanceState* instance_locked(XrInstance instance)
{
	auto it = L().instances.find(instance);
	return it == L().instances.end() ? nullptr : it->second.get();
}

// Synthetic events queued for the app's next xrPollEvent.
static void queue_profile_changed_locked(SessionState& s)
{
	XrEventDataBuffer buf{};
	auto* e = reinterpret_cast<XrEventDataInteractionProfileChanged*>(&buf);
	e->type = XR_TYPE_EVENT_DATA_INTERACTION_PROFILE_CHANGED;
	e->next = nullptr;
	e->session = s.handle;
	s.synthetic.push_back(buf);
}

static void queue_state_locked(SessionState& s, XrSessionState state)
{
	XrEventDataBuffer buf{};
	auto* e = reinterpret_cast<XrEventDataSessionStateChanged*>(&buf);
	e->type = XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED;
	e->next = nullptr;
	e->session = s.handle;
	e->state = state;
	e->time = s.last_display_time;
	s.synthetic.push_back(buf);
}

static void queue_recenter_locked(SessionState& s, XrReferenceSpaceType type)
{
	XrEventDataBuffer buf{};
	auto* e = reinterpret_cast<XrEventDataReferenceSpaceChangePending*>(&buf);
	e->type = XR_TYPE_EVENT_DATA_REFERENCE_SPACE_CHANGE_PENDING;
	e->next = nullptr;
	e->session = s.handle;
	e->referenceSpaceType = type;
	e->changeTime = s.last_display_time;
	e->poseValid = XR_FALSE;
	e->poseInPreviousSpace = pose_identity();
	s.synthetic.push_back(buf);
}

// ---------------------------------------------------------------------------
// Control commands

static std::string shm_name()
{
	std::string shm = env_str("VISOR_XR_SHM");
	return shm.empty() ? "/visor-xr-" + std::to_string(getpid()) : shm;
}

// The virtual headset publishes frames itself; if it does, the layer never
// needs to touch the app's swapchains.
static bool runtime_publishes_frames()
{
	if (L().capture_mode == "layer") {
		return false;
	}
	RuntimeFrames probe(shm_name());
	return probe.ok();
}

// XrTime -> CLOCK_MONOTONIC offset, from XR_KHR_convert_timespec_time.
static bool xr_time_offset(InstanceState* inst, int64_t* out)
{
	if (!inst || !inst->convert_timespec) {
		return false;
	}
	if (!inst->time_offset_known) {
		timespec ts{};
		clock_gettime(CLOCK_MONOTONIC, &ts);
		XrTime xt = 0;
		if (XR_FAILED(inst->convert_timespec(inst->handle, &ts, &xt))) {
			return false;
		}
		inst->time_offset = ((int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec) - xt;
		inst->time_offset_known = true;
	}
	*out = inst->time_offset;
	return true;
}

// Which of our frames a runtime capture shows, from its display time.
static void resolve_capture_frame_locked(SessionState& s, CaptureInfo& c)
{
	if (c.frame >= 0 || !s.capture || std::strcmp(s.capture->kind(), "runtime") != 0) {
		return;
	}
	int64_t offset;
	if (!xr_time_offset(s.inst, &offset)) {
		return;
	}
	int64_t xr_display = c.display_time - offset;
	int64_t best = -1, best_err = INT64_MAX;
	for (const auto& f : s.frames) {
		int64_t err = std::llabs(f.display_time - xr_display);
		if (err < best_err) {
			best_err = err;
			best = f.frame;
		}
	}
	int64_t tolerance = s.display_period > 0 ? s.display_period / 2 : 5000000;
	if (best >= 0 && best_err <= tolerance) {
		c.frame = best;
	}
}

// Block until the frame source has a picture of `target` (or later).
static CaptureInfo wait_for_frame(FrameSource* cap, XrSession session, int64_t target, int timeout_ms)
{
	auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
	bool runtime = std::strcmp(cap->kind(), "runtime") == 0;
	CaptureInfo info;
	while (true) {
		if (runtime) {
			uint64_t ticket = cap->request();
			cap->wait(ticket, 300, &info);
		} else {
			info = cap->latest();
		}
		{
			std::lock_guard<std::mutex> g(L().mu);
			if (SessionState* s = session_locked(session)) {
				resolve_capture_frame_locked(*s, info);
			}
		}
		if (info.frame >= target || std::chrono::steady_clock::now() >= deadline) {
			return info;
		}
		if (!runtime) {
			cap->wait(info.seq, 200, &info); // a newer frame, if one comes
		}
	}
}

// Frame capture is created on first use: until someone asks for frames, the
// layer never touches the app's Vulkan device.
static FrameSource* ensure_capture_locked(SessionState& s)
{
	if (s.capture) {
		return s.capture->ok() ? s.capture.get() : nullptr;
	}
	if (!L().capture_enabled) {
		return nullptr;
	}
	std::string shm = shm_name();
	if (L().capture_mode != "layer") {
		auto rt = std::make_unique<RuntimeFrames>(shm);
		if (rt->ok()) {
			s.capture = std::move(rt);
			LOGI("frames come from the runtime (shm %s)", shm.c_str());
		} else if (L().capture_mode == "runtime") {
			LOGW("frame capture unavailable: %s", rt->error().c_str());
			s.capture = std::move(rt);
			return nullptr;
		}
	}
	if (!s.capture) {
		if (!s.vulkan) {
			return nullptr;
		}
		s.capture.reset(new Capturer(s.vk, shm));
	}
	if (!s.capture->ok()) {
		LOGW("frame capture disabled: %s", s.capture->error().c_str());
		return nullptr;
	}
	s.capture->configure((uint32_t)env_num("VISOR_XR_CAPTURE_WIDTH", 960),
	                     env_str("VISOR_XR_CAPTURE_EYES", "left") == "both", env_num("VISOR_XR_CAPTURE_FPS", 0));
	// Callers that pause or step switch on every-frame capture themselves;
	// taking the gate lock here would deadlock "pause", which holds it.
	return s.capture.get();
}

static Json actions_json_locked(SessionState* s)
{
	Json list = Json::array();
	if (!s) {
		return list;
	}
	InstanceState* inst = s->inst;
	for (const auto& kv : inst->actions) {
		const ActionInfo& ai = kv.second;
		if (std::find(s->attached.begin(), s->attached.end(), ai.set) == s->attached.end()) {
			continue;
		}
		Json a = Json::object();
		a["name"] = ai.name;
		a["localized"] = ai.localized;
		auto set = inst->sets.find(ai.set);
		a["set"] = set != inst->sets.end() ? set->second.name : "";
		const char* type = "boolean";
		switch (ai.type) {
		case XR_ACTION_TYPE_FLOAT_INPUT: type = "float"; break;
		case XR_ACTION_TYPE_VECTOR2F_INPUT: type = "vector2"; break;
		case XR_ACTION_TYPE_POSE_INPUT: type = "pose"; break;
		case XR_ACTION_TYPE_VIBRATION_OUTPUT: type = "haptic"; break;
		default: break;
		}
		a["type"] = type;
		Json binds = Json::array();
		for (const auto& b : s->bindings) {
			if (b.action != kv.first) {
				continue;
			}
			Json bj = Json::object();
			bj["path"] = b.source.path;
			bj["hand"] = b.source.hand == kLeft ? "left" : "right";
			bj["input"] = input_name(b.source.input);
			bj["component"] = comp_name(b.source.comp);
			binds.push(bj);
		}
		a["bindings"] = binds;
		// Current synced state, so an agent can see what the app sees.
		auto st = s->synced.find({kv.first, -1});
		if (st != s->synced.end()) {
			Json state = Json::object();
			state["active"] = st->second.active;
			if (ai.type == XR_ACTION_TYPE_BOOLEAN_INPUT) {
				state["value"] = st->second.v.b;
			} else if (ai.type == XR_ACTION_TYPE_FLOAT_INPUT) {
				state["value"] = st->second.v.f;
			} else if (ai.type == XR_ACTION_TYPE_VECTOR2F_INPUT) {
				Json xy = Json::array();
				xy.push(st->second.v.x);
				xy.push(st->second.v.y);
				state["value"] = xy;
			}
			a["state"] = state;
		}
		list.push(a);
	}
	return list;
}

static Json stats_json_locked(const SessionState* s, int window_frames)
{
	Json j = Json::object();
	if (!s) {
		return j;
	}
	j["frames"] = (double)s->ends;
	j["display_period_ms"] = (double)s->display_period / 1e6;
	j["refresh_hz"] = s->display_period > 0 ? 1e9 / (double)s->display_period : 0.0;
	std::vector<double> intervals, cpu;
	int64_t missed = 0;
	int64_t first_end = 0, last_end = 0;
	int count = 0;
	const FrameRecord* prev = nullptr;
	size_t start = s->frames.size() > (size_t)window_frames ? s->frames.size() - window_frames : 0;
	for (size_t i = start; i < s->frames.size(); i++) {
		const FrameRecord& f = s->frames[i];
		if (!f.end) {
			continue;
		}
		if (!first_end) {
			first_end = f.end;
		}
		last_end = f.end;
		count++;
		if (f.begin && f.end > f.begin) {
			cpu.push_back((double)(f.end - f.wait_end) / 1e6);
		}
		if (f.gated) {
			prev = nullptr; // frozen or single-stepped: not the app's timing
			first_end = 0;
			count = 0;
			intervals.clear();
			cpu.clear();
			missed = 0;
			continue;
		}
		if (prev && prev->end) {
			intervals.push_back((double)(f.end - prev->end) / 1e6);
			if (f.period > 0 && prev->display_time > 0 && f.display_time > prev->display_time) {
				int64_t gaps = (f.display_time - prev->display_time + f.period / 2) / f.period - 1;
				if (gaps > 0) {
					missed += gaps;
				}
			}
		}
		prev = &f;
	}
	auto pct = [](std::vector<double> v, double p) {
		if (v.empty()) {
			return 0.0;
		}
		std::sort(v.begin(), v.end());
		size_t i = (size_t)std::min<double>((double)v.size() - 1, p * (double)(v.size() - 1) + 0.5);
		return v[i];
	};
	j["window_frames"] = count;
	j["fps"] = (count > 1 && last_end > first_end) ? (double)(count - 1) * 1e9 / (double)(last_end - first_end) : 0.0;
	j["frame_ms_p50"] = pct(intervals, 0.5);
	j["frame_ms_p95"] = pct(intervals, 0.95);
	j["frame_ms_p99"] = pct(intervals, 0.99);
	j["frame_ms_max"] = intervals.empty() ? 0.0 : *std::max_element(intervals.begin(), intervals.end());
	j["cpu_ms_p50"] = pct(cpu, 0.5);
	j["cpu_ms_p95"] = pct(cpu, 0.95);
	j["missed_frames"] = (double)missed;
	return j;
}

static Json frames_json_locked(const SessionState* s, int64_t since)
{
	Json rows = Json::array();
	if (!s) {
		return rows;
	}
	for (const auto& f : s->frames) {
		if (f.frame <= since || !f.end) {
			continue;
		}
		Json r = Json::array();
		r.push((double)f.frame);
		r.push((double)f.wait_start);
		r.push((double)f.wait_end);
		r.push((double)f.begin);
		r.push((double)f.end);
		r.push((double)f.display_time);
		r.push((double)f.period);
		r.push((double)f.layers);
		rows.push(r);
	}
	return rows;
}

static Json capture_json(const CaptureInfo& c, const std::string& shm)
{
	Json j = Json::object();
	j["seq"] = (double)c.seq;
	j["width"] = c.width;
	j["height"] = c.height;
	j["eyes"] = c.eyes;
	j["frame"] = (double)c.frame;
	j["display_time"] = (double)c.display_time;
	j["capture_ns"] = (double)c.capture_ns;
	j["shm"] = shm;
	return j;
}

static Json handle_command(const std::string& cmd, const Json& args, std::string& err)
{
	Layer& l = L();

	if (cmd == "ping") {
		Json j = Json::object();
		j["pong"] = true;
		return j;
	}

	if (cmd == "info" || cmd == "hello") {
		std::lock_guard<std::mutex> g(l.mu);
		Json j = Json::object();
		j["layer"] = VISOR_LAYER_NAME;
		j["version"] = VISOR_LAYER_VERSION;
		j["pid"] = (double)getpid();
		char exe[512] = {0};
		ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
		j["exe"] = n > 0 ? std::string(exe, (size_t)n) : std::string();
		j["drive"] = l.drive.load();
		j["socket"] = l.control ? l.control->path() : "";
		SessionState* s = session_locked(l.current);
		InstanceState* inst = s ? s->inst : (l.instances.empty() ? nullptr : l.instances.begin()->second.get());
		if (inst) {
			Json app = Json::object();
			app["name"] = inst->app_name;
			app["version"] = inst->app_version;
			app["engine"] = inst->engine_name;
			app["engine_version"] = inst->engine_version;
			char ver[32];
			std::snprintf(ver, sizeof(ver), "%u.%u.%u", (unsigned)XR_VERSION_MAJOR(inst->api_version),
			              (unsigned)XR_VERSION_MINOR(inst->api_version), (unsigned)XR_VERSION_PATCH(inst->api_version));
			app["api_version"] = ver;
			Json ext = Json::array();
			for (const auto& e : inst->extensions) {
				ext.push(e);
			}
			app["extensions"] = ext;
			j["app"] = app;
			j["runtime"] = inst->runtime_name;
		}
		if (s) {
			Json sj = Json::object();
			sj["state"] = session_state_name(s->state);
			sj["focused"] = focused_locked(*s);
			sj["running"] = s->running;
			sj["graphics"] = s->vulkan ? "vulkan" : "other";
			sj["frame"] = (double)s->render_frame;
			sj["refresh_hz"] = s->display_period > 0 ? 1e9 / (double)s->display_period : 0.0;
			sj["interaction_profile"] = s->profile;
			sj["view_count"] = s->view_count;
			Json sw = Json::array();
			for (const auto& kv : l.swapchains) {
				if (kv.second.session != s->handle) {
					continue;
				}
				Json w = Json::object();
				w["width"] = kv.second.info.width;
				w["height"] = kv.second.info.height;
				w["array"] = kv.second.info.arraySize;
				w["format"] = (double)kv.second.info.format;
				w["samples"] = kv.second.info.sampleCount;
				w["projection"] = !kv.second.views.empty();
				sw.push(w);
			}
			sj["swapchains"] = sw;
			if (s->capture) {
				Json c = Json::object();
				c["ok"] = s->capture->ok();
				c["error"] = s->capture->error();
				c["shm"] = s->capture->shm_name();
				c["eye_width"] = s->capture->eye_width();
				c["eyes"] = s->capture->both_eyes() ? "both" : "left";
				c["fps"] = s->capture->fps();
				sj["capture"] = c;
			}
			j["session"] = sj;
			j["actions"] = actions_json_locked(s);
		}
		return j;
	}

	if (cmd == "state") {
		Json j = l.rig.to_json();
		bool paused;
		{
			// Lock order is gate -> mu everywhere; read this before taking mu.
			std::lock_guard<std::mutex> gg(l.gate_mu);
			paused = l.paused;
		}
		std::lock_guard<std::mutex> g(l.mu);
		SessionState* s = session_locked(l.current);
		j["session"] = s ? session_state_name(s->state) : "none";
		j["focused"] = s ? focused_locked(*s) : false;
		j["render_frame"] = s ? (double)s->render_frame : 0.0;
		if (s) {
			Json views = Json::array();
			for (uint32_t i = 0; i < 2 && i < s->view_count; i++) {
				const XrFovf& f = s->last_fov[i];
				if (f.angleRight == 0.0f && f.angleLeft == 0.0f) {
					continue;
				}
				Json v = Json::object();
				Json fov = Json::array();
				fov.push(f.angleLeft / kDegToRad);
				fov.push(f.angleRight / kDegToRad);
				fov.push(f.angleUp / kDegToRad);
				fov.push(f.angleDown / kDegToRad);
				v["fov"] = fov;
				views.push(v);
			}
			j["views"] = views;
		}
		j["drive"] = l.drive.load();
		j["paused"] = paused;
		return j;
	}

	if (cmd == "set") {
		err = l.rig.queue(args);
		Json j = Json::object();
		j["frame"] = (double)l.rig.frame();
		return j;
	}

	if (cmd == "schedule") {
		// {"events": [{"frame": n, "set": {...}}], "relative": true}
		const Json* evs = args.get("events");
		if (!evs || !evs->is_array()) {
			err = "events must be an array of {frame, set}";
			return Json();
		}
		bool relative = args.get("relative") ? args.get("relative")->as_bool(true) : true;
		int64_t base = relative ? l.rig.frame() + 1 : 0;
		size_t n = 0;
		for (const auto& e : evs->items()) {
			const Json* set = e.get("set");
			if (!set) {
				continue;
			}
			int64_t f = e.get("frame") ? e.get("frame")->as_int(0) : 0;
			l.rig.schedule(base + f, *set);
			n++;
		}
		Json j = Json::object();
		j["scheduled"] = (double)n;
		j["start_frame"] = (double)base;
		return j;
	}

	if (cmd == "clear") {
		l.rig.clear_schedule();
		return Json::object();
	}

	if (cmd == "reset") {
		float h = (float)(args.get("eye_height") ? args.get("eye_height")->as_number(l.eye_height) : l.eye_height);
		l.rig.reset(h);
		return Json::object();
	}

	if (cmd == "record") {
		std::string action = args.get("action") ? args.get("action")->as_string("start") : "start";
		if (action == "start") {
			l.rig.start_recording();
			return Json::object();
		}
		return l.rig.stop_recording();
	}

	if (cmd == "drive") {
		bool on = args.get("on") ? args.get("on")->as_bool(true) : true;
		bool was = l.drive.exchange(on);
		if (on != was) {
			std::lock_guard<std::mutex> g(l.mu);
			if (SessionState* s = session_locked(l.current)) {
				queue_profile_changed_locked(*s);
			}
			Json e = Json::object();
			e["type"] = "drive";
			e["on"] = on;
			push_event(e);
		}
		Json j = Json::object();
		j["drive"] = on;
		return j;
	}

	if (cmd == "configure") {
		std::lock_guard<std::mutex> g(l.mu);
		SessionState* s = session_locked(l.current);
		FrameSource* cp = s ? ensure_capture_locked(*s) : nullptr;
		if (!cp) {
			err = s ? "this session can't be captured (needs Vulkan)" : "no session yet";
			return Json();
		}
		FrameSource& c = *cp;
		uint32_t w = (uint32_t)(args.get("capture_width") ? args.get("capture_width")->as_int(c.eye_width())
		                                                  : c.eye_width());
		bool both = c.both_eyes();
		if (const Json* e = args.get("eyes")) {
			both = e->as_string("left") == "both";
		}
		double fps = args.get("capture_fps") ? args.get("capture_fps")->as_number(c.fps()) : c.fps();
		c.configure(w, both, fps);
		Json j = Json::object();
		j["capture_width"] = c.eye_width();
		j["eyes"] = c.both_eyes() ? "both" : "left";
		j["capture_fps"] = c.fps();
		return j;
	}

	if (cmd == "capture") {
		FrameSource* c = nullptr;
		std::string shm;
		{
			std::lock_guard<std::mutex> g(l.mu);
			SessionState* s = session_locked(l.current);
			c = s ? ensure_capture_locked(*s) : nullptr;
			if (!c) {
				err = s && s->capture ? "capture unavailable: " + s->capture->error()
				                      : "no Vulkan session to capture (yet)";
				return Json();
			}
			shm = c->shm_name();
		}
		bool paused;
		{
			std::lock_guard<std::mutex> gg(l.gate_mu);
			paused = l.paused && l.step_budget == 0;
		}
		bool fresh = args.get("fresh") ? args.get("fresh")->as_bool(true) : true;
		int timeout = (int)(args.get("timeout_ms") ? args.get("timeout_ms")->as_int(3000) : 3000);
		bool runtime = std::strcmp(c->kind(), "runtime") == 0;
		CaptureInfo info;
		if (runtime && fresh && !paused) {
			// The compositor keeps presenting the last frame, even when paused.
			uint64_t ticket = c->request();
			if (!c->wait(ticket, timeout, &info)) {
				err = "timed out waiting for a frame (is the app rendering?)";
				return Json();
			}
		} else if (!fresh || paused) {
			// Paused: the newest frame already shows the current state.
			info = c->latest();
			if (info.seq == 0 && (paused || !c->wait(0, timeout, &info))) {
				err = paused ? "no frame captured since pausing — step the app once" : "no frame captured yet";
				return Json();
			}
		} else {
			uint64_t after = c->request();
			if (!c->wait(after, timeout, &info)) {
				err = "timed out waiting for a frame (is the app rendering?)";
				return Json();
			}
		}
		{
			std::lock_guard<std::mutex> g(l.mu);
			if (SessionState* s = session_locked(l.current)) {
				resolve_capture_frame_locked(*s, info);
			}
		}
		Json j = capture_json(info, shm);
		j["paused"] = paused;
		j["source"] = c->kind();
		return j;
	}

	if (cmd == "pause") {
		// Capture every frame while paused, and make sure there is a picture
		// of the moment we froze (the null compositor only sees frames the app
		// submits, and a paused app submits none).
		FrameSource* c = nullptr;
		bool running = false;
		{
			std::lock_guard<std::mutex> g(l.mu);
			if (SessionState* s = session_locked(l.current)) {
				c = ensure_capture_locked(*s);
				running = s->running;
			}
		}
		bool already;
		{
			std::lock_guard<std::mutex> gg(l.gate_mu);
			already = l.paused && l.step_budget == 0;
		}
		if (c && running && !already) {
			c->set_every_frame(true);
			CaptureInfo info;
			c->wait(c->request(), 1000, &info);
		}
		{
			std::lock_guard<std::mutex> gg(l.gate_mu);
			l.paused = true;
			l.step_budget = 0;
		}
		Json j = Json::object();
		j["paused"] = true;
		return j;
	}

	if (cmd == "resume") {
		{
			std::lock_guard<std::mutex> gg(l.gate_mu);
			l.paused = false;
			l.step_budget = 0;
		}
		l.gate_cv.notify_all();
		std::lock_guard<std::mutex> g(l.mu);
		if (SessionState* s = session_locked(l.current)) {
			if (s->capture) {
				s->capture->set_every_frame(false);
			}
		}
		Json j = Json::object();
		j["paused"] = false;
		return j;
	}

	if (cmd == "step") {
		int64_t frames = args.get("frames") ? args.get("frames")->as_int(1) : 1;
		if (frames < 1) {
			frames = 1;
		}
		int timeout = (int)(args.get("timeout_ms") ? args.get("timeout_ms")->as_int(10000) : 10000);
		int64_t target;
		XrSession step_session = XR_NULL_HANDLE;
		FrameSource* cap = nullptr;
		{
			std::lock_guard<std::mutex> g(l.mu);
			SessionState* s = session_locked(l.current);
			if (!s || !s->running) {
				err = "no running session to step";
				return Json();
			}
			target = s->waits + frames;
			step_session = s->handle;
			cap = ensure_capture_locked(*s);
			if (cap) {
				cap->set_every_frame(true);
			}
		}
		{
			std::lock_guard<std::mutex> gg(l.gate_mu);
			l.paused = true;
			l.step_budget += frames;
		}
		l.gate_cv.notify_all();
		// Wait until the stepped frames have been ended (rendered).
		int64_t ended = 0;
		{
			std::unique_lock<std::mutex> gg(l.gate_mu);
			l.frame_cv.wait_for(gg, std::chrono::milliseconds(timeout), [&] {
				std::lock_guard<std::mutex> g(l.mu);
				SessionState* s = session_locked(l.current);
				ended = s ? s->ended_frame : 0;
				return !s || s->ended_frame >= target || l.shutting_down;
			});
		}
		Json j = Json::object();
		j["frame"] = (double)ended;
		j["target"] = (double)target;
		j["complete"] = ended >= target;
		// And until that frame's pixels are published, so a capture shows it.
		if (cap && cap->ok() && ended >= target) {
			CaptureInfo info = wait_for_frame(cap, step_session, target, 2000);
			j["capture_frame"] = (double)info.frame;
		}
		return j;
	}

	if (cmd == "events") {
		uint64_t since = (uint64_t)(args.get("since") ? args.get("since")->as_int(0) : 0);
		Json list = Json::array();
		std::lock_guard<std::mutex> g(l.ev_mu);
		for (const auto& e : l.events) {
			if ((uint64_t)e.get("seq")->as_int(0) > since) {
				list.push(e);
			}
		}
		Json j = Json::object();
		j["events"] = list;
		j["latest"] = (double)l.ev_seq;
		return j;
	}

	if (cmd == "stats") {
		int window = (int)(args.get("window") ? args.get("window")->as_int(300) : 300);
		std::lock_guard<std::mutex> g(l.mu);
		SessionState* s = session_locked(l.current);
		Json j = stats_json_locked(s, window);
		if (args.get("frames_since")) {
			j["frame_rows"] = frames_json_locked(s, args.get("frames_since")->as_int(0));
			j["frame_columns"] = "frame,wait_start,wait_end,begin,end,display_time,period,layers";
		}
		return j;
	}

	if (cmd == "session") {
		std::string ev = args.get("event") ? args.get("event")->as_string("") : "";
		XrSession handle = XR_NULL_HANDLE;
		InstanceState* inst = nullptr;
		{
			std::lock_guard<std::mutex> g(l.mu);
			SessionState* s = session_locked(l.current);
			if (!s) {
				err = "no session";
				return Json();
			}
			handle = s->handle;
			inst = s->inst;
			if (ev == "focus_lost") {
				if (s->state == XR_SESSION_STATE_FOCUSED && !s->synthetic_unfocused) {
					s->synthetic_unfocused = true;
					queue_state_locked(*s, XR_SESSION_STATE_VISIBLE);
				}
			} else if (ev == "focus_gained") {
				if (s->synthetic_unfocused) {
					s->synthetic_unfocused = false;
					queue_state_locked(*s, XR_SESSION_STATE_FOCUSED);
				}
			} else if (ev == "recenter") {
				l.rig.queue(Json::parse("{\"recenter\":true}"));
				queue_recenter_locked(*s, XR_REFERENCE_SPACE_TYPE_LOCAL);
				queue_recenter_locked(*s, XR_REFERENCE_SPACE_TYPE_LOCAL_FLOOR);
			} else if (ev != "exit") {
				err = "event must be focus_lost, focus_gained, recenter or exit";
				return Json();
			}
		}
		if (ev == "exit") {
			XrResult r = inst->next.RequestExitSession(handle);
			if (XR_FAILED(r)) {
				err = "xrRequestExitSession failed (" + std::to_string((int)r) + ")";
				return Json();
			}
		}
		Json e = Json::object();
		e["type"] = "injected";
		e["event"] = ev;
		push_event(e);
		return Json::object();
	}

	err = "unknown command '" + cmd + "'";
	return Json();
}

static void ensure_control_server()
{
	Layer& l = L();
	std::lock_guard<std::mutex> g(l.mu);
	if (l.control || env_str("VISOR_XR_NO_SOCKET") == "1") {
		return;
	}
	l.control.reset(new ControlServer(handle_command));
	if (!l.control->start()) {
		l.control.reset();
	}
}

static void read_config()
{
	Layer& l = L();
	l.eye_height = (float)env_num("VISOR_XR_EYE_HEIGHT", 1.65);
	l.rig.reset(l.eye_height);
	l.drive = env_str("VISOR_XR_MODE", "drive") != "observe";
	l.system_name = env_str("VISOR_XR_SYSTEM_NAME");
	l.force_profile = env_str("VISOR_XR_PROFILE");
	std::string cap = env_str("VISOR_XR_CAPTURE", "auto");
	l.capture_enabled = cap != "0" && cap != "off";
	l.capture_mode = (cap == "layer" || cap == "runtime") ? cap : "auto";
	std::string fov = env_str("VISOR_XR_FOV"); // outer,inner,up,down degrees (left eye)
	float o, i, u, d;
	if (!fov.empty() && std::sscanf(fov.c_str(), "%f,%f,%f,%f", &o, &i, &u, &d) == 4) {
		l.fov_override = true;
		l.fov_left = XrFovf{-o * kDegToRad, i * kDegToRad, u * kDegToRad, -d * kDegToRad};
	}
	if (env_str("VISOR_XR_PAUSED") == "1") {
		// Not from the very first frame: the session only becomes visible and
		// focused once the app submits frames, and a frozen black screen helps
		// nobody. Freeze a few frames after focus instead.
		l.pause_on_focus = true;
	}
}

// ---------------------------------------------------------------------------
// Intercepted entry points

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrGetInstanceProcAddr(XrInstance instance, const char* name,
                                                                 PFN_xrVoidFunction* function);

#define NEXT(inst) (inst)->next

static InstanceState* inst_of_session(XrSession session)
{
	std::lock_guard<std::mutex> g(L().mu);
	SessionState* s = session_locked(session);
	return s ? s->inst : nullptr;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrDestroyInstance(XrInstance instance)
{
	InstanceState* inst;
	{
		std::lock_guard<std::mutex> g(L().mu);
		inst = instance_locked(instance);
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	PFN_xrDestroyInstance destroy = inst->next.DestroyInstance;
	XrResult r = destroy(instance);
	std::unique_ptr<ControlServer> server;
	{
		std::lock_guard<std::mutex> g(L().mu);
		L().instances.erase(instance);
		if (L().instances.empty()) {
			// The loader may unload us after this; no thread of ours may outlive
			// it. (A later instance starts a fresh server on the same socket.)
			server = std::move(L().control);
		}
	}
	if (server) {
		server->stop();
	}
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrGetSystemProperties(XrInstance instance, XrSystemId systemId,
                                                                 XrSystemProperties* properties)
{
	InstanceState* inst;
	{
		std::lock_guard<std::mutex> g(L().mu);
		inst = instance_locked(instance);
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).GetSystemProperties(instance, systemId, properties);
	if (XR_SUCCEEDED(r) && !L().system_name.empty()) {
		std::snprintf(properties->systemName, XR_MAX_SYSTEM_NAME_SIZE, "%s", L().system_name.c_str());
	}
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrPollEvent(XrInstance instance, XrEventDataBuffer* eventData)
{
	InstanceState* inst;
	{
		std::lock_guard<std::mutex> g(L().mu);
		inst = instance_locked(instance);
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).PollEvent(instance, eventData);
	Layer& l = L();
	if (r == XR_SUCCESS && eventData->type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
		auto* e = reinterpret_cast<XrEventDataSessionStateChanged*>(eventData);
		std::lock_guard<std::mutex> g(l.mu);
		if (SessionState* s = session_locked(e->session)) {
			s->state = e->state;
			if (e->state != XR_SESSION_STATE_FOCUSED) {
				s->synthetic_unfocused = false;
			}
			Json ev = Json::object();
			ev["type"] = "session_state";
			ev["state"] = session_state_name(e->state);
			push_event(ev);
			LOGI("session state -> %s", session_state_name(e->state));
		}
		return r;
	}
	if (r == XR_EVENT_UNAVAILABLE) {
		std::lock_guard<std::mutex> g(l.mu);
		for (auto& kv : l.sessions) {
			SessionState& s = *kv.second;
			if (s.inst != inst || s.synthetic.empty()) {
				continue;
			}
			*eventData = s.synthetic.front();
			s.synthetic.pop_front();
			if (eventData->type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
				auto* e = reinterpret_cast<XrEventDataSessionStateChanged*>(eventData);
				Json ev = Json::object();
				ev["type"] = "session_state";
				ev["state"] = session_state_name(e->state);
				ev["synthetic"] = true;
				push_event(ev);
			}
			return XR_SUCCESS;
		}
	}
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrCreateSession(XrInstance instance, const XrSessionCreateInfo* createInfo,
                                                           XrSession* session)
{
	InstanceState* inst;
	{
		std::lock_guard<std::mutex> g(L().mu);
		inst = instance_locked(instance);
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).CreateSession(instance, createInfo, session);
	if (XR_FAILED(r)) {
		return r;
	}
	auto s = std::make_unique<SessionState>();
	s->handle = *session;
	s->inst = inst;
	for (auto* n = reinterpret_cast<const XrBaseInStructure*>(createInfo->next); n; n = n->next) {
		if (n->type == XR_TYPE_GRAPHICS_BINDING_VULKAN_KHR) {
			s->vulkan = true;
			s->vk = *reinterpret_cast<const XrGraphicsBindingVulkanKHR*>(n);
			s->vk.next = nullptr;
		}
	}
	if (!s->vulkan) {
		LOGI("session is not Vulkan; poses and input are driven, frames are not captured");
	}
	bool capture_now = s->vulkan && env_num("VISOR_XR_CAPTURE_FPS", 0) > 0;
	{
		std::lock_guard<std::mutex> g(L().mu);
		L().sessions[*session] = std::move(s);
		L().current = *session;
		if (capture_now) {
			ensure_capture_locked(*L().sessions[*session]);
		}
	}
	Json ev = Json::object();
	ev["type"] = "session_created";
	push_event(ev);
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrDestroySession(XrSession session)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	std::unique_ptr<FrameSource> cap;
	{
		std::lock_guard<std::mutex> g(L().mu);
		if (SessionState* s = session_locked(session)) {
			cap = std::move(s->capture);
		}
	}
	if (cap) {
		cap->shutdown(); // before the runtime (and app) let the device go
	}
	XrResult r = NEXT(inst).DestroySession(session);
	Layer& l = L();
	{
		std::lock_guard<std::mutex> g(l.mu);
		for (auto it = l.spaces.begin(); it != l.spaces.end();) {
			it = it->second.session == session ? l.spaces.erase(it) : std::next(it);
		}
		for (auto it = l.swapchains.begin(); it != l.swapchains.end();) {
			it = it->second.session == session ? l.swapchains.erase(it) : std::next(it);
		}
		l.sessions.erase(session);
		if (l.current == session) {
			l.current = l.sessions.empty() ? XR_NULL_HANDLE : l.sessions.begin()->first;
		}
	}
	l.frame_cv.notify_all();
	Json ev = Json::object();
	ev["type"] = "session_destroyed";
	push_event(ev);
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrBeginSession(XrSession session, const XrSessionBeginInfo* info)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).BeginSession(session, info);
	if (XR_SUCCEEDED(r)) {
		std::lock_guard<std::mutex> g(L().mu);
		if (SessionState* s = session_locked(session)) {
			s->running = true;
		}
	}
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrEndSession(XrSession session)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	{
		// Never leave the app parked in xrWaitFrame while it shuts down.
		std::lock_guard<std::mutex> g(L().mu);
		if (SessionState* s = session_locked(session)) {
			s->running = false;
		}
	}
	L().gate_cv.notify_all();
	return NEXT(inst).EndSession(session);
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrCreateReferenceSpace(XrSession session,
                                                                  const XrReferenceSpaceCreateInfo* info,
                                                                  XrSpace* space)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).CreateReferenceSpace(session, info, space);
	if (XR_SUCCEEDED(r)) {
		SpaceInfo sp;
		sp.session = session;
		sp.kind = SpaceInfo::Reference;
		sp.ref_type = info->referenceSpaceType;
		sp.offset = info->poseInReferenceSpace;
		std::lock_guard<std::mutex> g(L().mu);
		L().spaces[*space] = sp;
	}
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrCreateActionSpace(XrSession session, const XrActionSpaceCreateInfo* info,
                                                               XrSpace* space)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).CreateActionSpace(session, info, space);
	if (XR_SUCCEEDED(r)) {
		SpaceInfo sp;
		sp.session = session;
		sp.kind = SpaceInfo::Action;
		sp.action = info->action;
		sp.hand = hand_of(inst, info->subactionPath);
		sp.offset = info->poseInActionSpace;
		std::lock_guard<std::mutex> g(L().mu);
		L().spaces[*space] = sp;
	}
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrDestroySpace(XrSpace space)
{
	InstanceState* inst = nullptr;
	{
		std::lock_guard<std::mutex> g(L().mu);
		auto it = L().spaces.find(space);
		if (it != L().spaces.end()) {
			SessionState* s = session_locked(it->second.session);
			inst = s ? s->inst : nullptr;
			L().spaces.erase(it);
		}
		if (!inst && !L().instances.empty()) {
			inst = L().instances.begin()->second.get();
		}
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	return NEXT(inst).DestroySpace(space);
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrLocateSpace(XrSpace space, XrSpace baseSpace, XrTime time,
                                                         XrSpaceLocation* location)
{
	InstanceState* inst = nullptr;
	if (L().drive) {
		std::lock_guard<std::mutex> g(L().mu);
		XrSpaceVelocity* vel = nullptr;
		for (auto* n = reinterpret_cast<XrBaseOutStructure*>(location->next); n; n = n->next) {
			if (n->type == XR_TYPE_SPACE_VELOCITY) {
				vel = reinterpret_cast<XrSpaceVelocity*>(n);
			}
		}
		XrSpaceLocationFlags flags = 0;
		XrPosef pose = pose_identity();
		XrSpaceVelocityFlags vflags = 0;
		XrVector3f lin{}, ang{};
		if (locate_locked(space, baseSpace, &flags, &pose, vel ? &vflags : nullptr, &lin, &ang)) {
			location->locationFlags = flags;
			location->pose = pose;
			if (vel) {
				vel->velocityFlags = vflags;
				vel->linearVelocity = lin;
				vel->angularVelocity = ang;
			}
			return XR_SUCCESS;
		}
	}
	{
		std::lock_guard<std::mutex> g(L().mu);
		auto it = L().spaces.find(space);
		if (it != L().spaces.end()) {
			SessionState* s = session_locked(it->second.session);
			inst = s ? s->inst : nullptr;
		}
		if (!inst && !L().instances.empty()) {
			inst = L().instances.begin()->second.get();
		}
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	return NEXT(inst).LocateSpace(space, baseSpace, time, location);
}

static XrResult locate_spaces_common(XrSession session, const XrSpacesLocateInfo* info, XrSpaceLocations* out,
                                     PFN_xrLocateSpaces next_fn)
{
	XrResult r = next_fn(session, info, out);
	if (XR_FAILED(r) || !L().drive) {
		return r;
	}
	XrSpaceVelocities* vels = nullptr;
	for (auto* n = reinterpret_cast<XrBaseOutStructure*>(out->next); n; n = n->next) {
		if (n->type == XR_TYPE_SPACE_VELOCITIES) {
			vels = reinterpret_cast<XrSpaceVelocities*>(n);
		}
	}
	std::lock_guard<std::mutex> g(L().mu);
	for (uint32_t i = 0; i < info->spaceCount && i < out->locationCount; i++) {
		XrSpaceLocationFlags flags = 0;
		XrPosef pose = pose_identity();
		XrSpaceVelocityFlags vflags = 0;
		XrVector3f lin{}, ang{};
		bool want_vel = vels && i < vels->velocityCount;
		if (locate_locked(info->spaces[i], info->baseSpace, &flags, &pose, want_vel ? &vflags : nullptr, &lin,
		                  &ang)) {
			out->locations[i].locationFlags = flags;
			out->locations[i].pose = pose;
			if (want_vel) {
				vels->velocities[i].velocityFlags = vflags;
				vels->velocities[i].linearVelocity = lin;
				vels->velocities[i].angularVelocity = ang;
			}
		}
	}
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrLocateSpaces(XrSession session, const XrSpacesLocateInfo* info,
                                                          XrSpaceLocations* out)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst || !inst->next.LocateSpaces) {
		return XR_ERROR_HANDLE_INVALID;
	}
	return locate_spaces_common(session, info, out, inst->next.LocateSpaces);
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrLocateSpacesKHR(XrSession session, const XrSpacesLocateInfo* info,
                                                             XrSpaceLocations* out)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst || !inst->next.LocateSpacesKHR) {
		return XR_ERROR_HANDLE_INVALID;
	}
	return locate_spaces_common(session, info, out, inst->next.LocateSpacesKHR);
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrLocateViews(XrSession session, const XrViewLocateInfo* info,
                                                         XrViewState* viewState, uint32_t capacity, uint32_t* count,
                                                         XrView* views)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).LocateViews(session, info, viewState, capacity, count, views);
	if (XR_FAILED(r) || capacity == 0 || !views) {
		return r;
	}
	Layer& l = L();
	std::lock_guard<std::mutex> g(l.mu);
	if (SessionState* s = session_locked(session)) {
		for (uint32_t i = 0; i < std::min(capacity, *count) && i < 2; i++) {
			s->last_fov[i] = l.fov_override ? (i == 0 ? l.fov_left
			                                          : XrFovf{-l.fov_left.angleRight, -l.fov_left.angleLeft,
			                                                   l.fov_left.angleUp, l.fov_left.angleDown})
			                                : views[i].fov;
		}
	}
	if (!l.drive) {
		return r;
	}
	auto it = l.spaces.find(info->space);
	if (it == l.spaces.end()) {
		return r;
	}
	RigState rig = l.rig.snapshot();
	XrPosef base;
	bool tracked;
	if (!space_pose_locked(it->second, rig, &base, &tracked)) {
		return r;
	}
	uint32_t n = std::min(capacity, *count);
	if (SessionState* s = session_locked(session)) {
		s->view_count = n;
	}
	for (uint32_t i = 0; i < n; i++) {
		float x = n == 2 ? (i == 0 ? -rig.ipd * 0.5f : rig.ipd * 0.5f) : 0.0f;
		XrPosef eye = compose(rig.head, XrPosef{quat_identity(), {x, 0.0f, 0.0f}});
		views[i].pose = relative(base, eye);
		if (l.fov_override) {
			XrFovf f = l.fov_left;
			if (n == 2 && i == 1) {
				f = XrFovf{-l.fov_left.angleRight, -l.fov_left.angleLeft, l.fov_left.angleUp, l.fov_left.angleDown};
			}
			views[i].fov = f;
		}
	}
	viewState->viewStateFlags = XR_VIEW_STATE_ORIENTATION_VALID_BIT | XR_VIEW_STATE_POSITION_VALID_BIT |
	                            XR_VIEW_STATE_ORIENTATION_TRACKED_BIT | XR_VIEW_STATE_POSITION_TRACKED_BIT;
	return r;
}

// --- swapchains ------------------------------------------------------------

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrCreateSwapchain(XrSession session, const XrSwapchainCreateInfo* info,
                                                             XrSwapchain* swapchain)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrSwapchainCreateInfo ci = *info;
	// Only the layer's own capture reads swapchain images on the app's queue;
	// when the runtime publishes frames, leave the app's request untouched.
	static const bool add_transfer = L().capture_enabled && !runtime_publishes_frames();
	if (add_transfer && (ci.usageFlags & XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT)) {
		ci.usageFlags |= XR_SWAPCHAIN_USAGE_TRANSFER_SRC_BIT; // so frames can be copied out
	}
	XrResult r = NEXT(inst).CreateSwapchain(session, &ci, swapchain);
	if (XR_FAILED(r) && ci.usageFlags != info->usageFlags) {
		r = NEXT(inst).CreateSwapchain(session, info, swapchain); // runtime refused the extra bit
		ci = *info;
	}
	if (XR_SUCCEEDED(r)) {
		SwapchainInfo sw;
		sw.session = session;
		sw.info = ci;
		sw.info.next = nullptr;
		std::lock_guard<std::mutex> g(L().mu);
		L().swapchains[*swapchain] = sw;
	}
	return r;
}

static InstanceState* inst_of_swapchain(XrSwapchain swapchain, SwapchainInfo** out = nullptr)
{
	auto it = L().swapchains.find(swapchain);
	if (it == L().swapchains.end()) {
		return nullptr;
	}
	if (out) {
		*out = &it->second;
	}
	SessionState* s = session_locked(it->second.session);
	return s ? s->inst : nullptr;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrDestroySwapchain(XrSwapchain swapchain)
{
	InstanceState* inst;
	{
		std::lock_guard<std::mutex> g(L().mu);
		inst = inst_of_swapchain(swapchain);
		L().swapchains.erase(swapchain);
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	return NEXT(inst).DestroySwapchain(swapchain);
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrEnumerateSwapchainImages(XrSwapchain swapchain, uint32_t capacity,
                                                                      uint32_t* count,
                                                                      XrSwapchainImageBaseHeader* images)
{
	InstanceState* inst;
	{
		std::lock_guard<std::mutex> g(L().mu);
		inst = inst_of_swapchain(swapchain);
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).EnumerateSwapchainImages(swapchain, capacity, count, images);
	if (XR_SUCCEEDED(r) && capacity > 0 && images && images->type == XR_TYPE_SWAPCHAIN_IMAGE_VULKAN_KHR) {
		auto* vk = reinterpret_cast<XrSwapchainImageVulkanKHR*>(images);
		std::lock_guard<std::mutex> g(L().mu);
		SwapchainInfo* sw = nullptr;
		if (inst_of_swapchain(swapchain, &sw) && sw) {
			sw->images.clear();
			for (uint32_t i = 0; i < *count; i++) {
				sw->images.push_back(vk[i].image);
			}
		}
	}
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrAcquireSwapchainImage(XrSwapchain swapchain,
                                                                   const XrSwapchainImageAcquireInfo* info,
                                                                   uint32_t* index)
{
	InstanceState* inst;
	{
		std::lock_guard<std::mutex> g(L().mu);
		inst = inst_of_swapchain(swapchain);
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).AcquireSwapchainImage(swapchain, info, index);
	if (XR_SUCCEEDED(r)) {
		std::lock_guard<std::mutex> g(L().mu);
		SwapchainInfo* sw = nullptr;
		if (inst_of_swapchain(swapchain, &sw) && sw) {
			sw->acquired.push_back(*index);
		}
	}
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrReleaseSwapchainImage(XrSwapchain swapchain,
                                                                   const XrSwapchainImageReleaseInfo* info)
{
	InstanceState* inst = nullptr;
	FrameSource* cap = nullptr;
	VkImage image = VK_NULL_HANDLE;
	VkFormat format = VK_FORMAT_UNDEFINED;
	uint32_t w = 0, h = 0;
	std::vector<CaptureView> views;
	int64_t frame = 0;
	{
		std::lock_guard<std::mutex> g(L().mu);
		SwapchainInfo* sw = nullptr;
		inst = inst_of_swapchain(swapchain, &sw);
		if (inst && sw && !sw->acquired.empty()) {
			uint32_t idx = sw->acquired.front();
			sw->acquired.pop_front();
			SessionState* s = session_locked(sw->session);
			if (s && s->capture && !sw->views.empty() && idx < sw->images.size() && sw->info.sampleCount <= 1) {
				cap = s->capture.get();
				image = sw->images[idx];
				format = (VkFormat)sw->info.format;
				w = sw->info.width;
				h = sw->info.height;
				views = sw->views;
				frame = s->render_frame;
			}
		}
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	if (cap) {
		cap->on_release(image, format, w, h, views, frame);
	}
	return NEXT(inst).ReleaseSwapchainImage(swapchain, info);
}

// --- frame loop ------------------------------------------------------------

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrWaitFrame(XrSession session, const XrFrameWaitInfo* info,
                                                       XrFrameState* state)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	Layer& l = L();
	if (l.pause_on_focus) {
		bool focused = false;
		FrameSource* cap = nullptr;
		{
			std::lock_guard<std::mutex> g(l.mu);
			if (SessionState* s = session_locked(session)) {
				focused = s->state == XR_SESSION_STATE_FOCUSED;
				cap = ensure_capture_locked(*s);
			}
		}
		if (cap) {
			cap->set_every_frame(true);
		}
		std::lock_guard<std::mutex> gg(l.gate_mu);
		if (focused && ++l.focused_frames >= 3) {
			l.paused = true;
			l.pause_on_focus = false;
			LOGI("paused at the first focused frames, as asked (VISOR_XR_PAUSED)");
		}
	}
	bool gated = false;
	{
		// The pause gate: an agent holding the world still between steps.
		std::unique_lock<std::mutex> gg(l.gate_mu);
		gated = l.paused;
		l.gate_cv.wait(gg, [&] {
			if (!l.paused || l.step_budget > 0 || l.shutting_down) {
				return true;
			}
			std::lock_guard<std::mutex> g(l.mu);
			SessionState* s = session_locked(session);
			return !s || !s->running;
		});
		if (l.paused && l.step_budget > 0) {
			l.step_budget--;
		}
	}
	int64_t t0 = now_ns();
	XrResult r = NEXT(inst).WaitFrame(session, info, state);
	int64_t t1 = now_ns();
	if (XR_FAILED(r)) {
		return r;
	}
	int64_t frame;
	{
		std::lock_guard<std::mutex> g(l.mu);
		SessionState* s = session_locked(session);
		if (!s) {
			return r;
		}
		frame = ++s->waits;
		s->last_display_time = state->predictedDisplayTime;
		s->display_period = state->predictedDisplayPeriod;
		FrameRecord rec;
		rec.frame = frame;
		rec.wait_start = t0;
		rec.wait_end = t1;
		rec.display_time = state->predictedDisplayTime;
		rec.period = state->predictedDisplayPeriod;
		rec.gated = gated;
		s->frames.push_back(rec);
		while (s->frames.size() > 4096) {
			s->frames.pop_front();
		}
	}
	l.rig.advance_frame(frame, t1);
	return r;
}

static FrameRecord* record_for(SessionState* s, int64_t frame)
{
	for (auto it = s->frames.rbegin(); it != s->frames.rend(); ++it) {
		if (it->frame == frame) {
			return &*it;
		}
		if (it->frame < frame) {
			break;
		}
	}
	return nullptr;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrBeginFrame(XrSession session, const XrFrameBeginInfo* info)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).BeginFrame(session, info);
	if (XR_SUCCEEDED(r)) {
		std::lock_guard<std::mutex> g(L().mu);
		if (SessionState* s = session_locked(session)) {
			s->render_frame = ++s->begins;
			if (FrameRecord* rec = record_for(s, s->render_frame)) {
				rec->begin = now_ns();
			}
		}
	}
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrEndFrame(XrSession session, const XrFrameEndInfo* info)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	Layer& l = L();
	FrameSource* cap = nullptr;
	int64_t frame = 0;
	{
		std::lock_guard<std::mutex> g(l.mu);
		SessionState* s = session_locked(session);
		if (s) {
			frame = s->render_frame;
			cap = s->capture.get();
			// Learn which swapchain rectangles hold the eyes, for next release.
			std::map<XrSwapchain, std::vector<CaptureView>> seen;
			for (uint32_t i = 0; i < info->layerCount; i++) {
				const XrCompositionLayerBaseHeader* layer = info->layers[i];
				if (!layer || layer->type != XR_TYPE_COMPOSITION_LAYER_PROJECTION) {
					continue;
				}
				auto* proj = reinterpret_cast<const XrCompositionLayerProjection*>(layer);
				for (uint32_t v = 0; v < proj->viewCount; v++) {
					const XrSwapchainSubImage& sub = proj->views[v].subImage;
					seen[sub.swapchain].push_back(CaptureView{(int)v, sub.imageArrayIndex, sub.imageRect});
				}
				break; // the first projection layer is the scene
			}
			for (auto& kv : seen) {
				auto it = l.swapchains.find(kv.first);
				if (it != l.swapchains.end()) {
					it->second.views = kv.second;
				}
			}
			if (FrameRecord* rec = record_for(s, frame)) {
				rec->layers = info->layerCount;
				rec->rendered = info->layerCount > 0;
			}
		}
	}
	if (cap) {
		cap->on_end_frame(frame, info->displayTime);
	}
	XrResult r = NEXT(inst).EndFrame(session, info);
	{
		std::lock_guard<std::mutex> g(l.mu);
		if (SessionState* s = session_locked(session)) {
			s->ends++;
			s->ended_frame = frame;
			if (FrameRecord* rec = record_for(s, frame)) {
				rec->end = now_ns();
			}
		}
	}
	{
		std::lock_guard<std::mutex> gg(l.gate_mu);
	}
	l.frame_cv.notify_all();
	return r;
}

// --- actions ---------------------------------------------------------------

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrCreateActionSet(XrInstance instance, const XrActionSetCreateInfo* info,
                                                             XrActionSet* set)
{
	InstanceState* inst;
	{
		std::lock_guard<std::mutex> g(L().mu);
		inst = instance_locked(instance);
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).CreateActionSet(instance, info, set);
	if (XR_SUCCEEDED(r)) {
		std::lock_guard<std::mutex> g(L().mu);
		inst->sets[*set] = ActionSetInfo{info->actionSetName, info->localizedActionSetName, info->priority};
	}
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrDestroyActionSet(XrActionSet set)
{
	InstanceState* inst = nullptr;
	{
		std::lock_guard<std::mutex> g(L().mu);
		for (auto& kv : L().instances) {
			if (kv.second->sets.erase(set)) {
				inst = kv.second.get();
				for (auto it = inst->actions.begin(); it != inst->actions.end();) {
					it = it->second.set == set ? inst->actions.erase(it) : std::next(it);
				}
				break;
			}
		}
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	return NEXT(inst).DestroyActionSet(set);
}

static InstanceState* inst_of_set_locked(XrActionSet set)
{
	for (auto& kv : L().instances) {
		if (kv.second->sets.count(set)) {
			return kv.second.get();
		}
	}
	return nullptr;
}

static InstanceState* inst_of_action_locked(XrAction action)
{
	for (auto& kv : L().instances) {
		if (kv.second->actions.count(action)) {
			return kv.second.get();
		}
	}
	return nullptr;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrCreateAction(XrActionSet set, const XrActionCreateInfo* info,
                                                          XrAction* action)
{
	InstanceState* inst;
	{
		std::lock_guard<std::mutex> g(L().mu);
		inst = inst_of_set_locked(set);
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).CreateAction(set, info, action);
	if (XR_SUCCEEDED(r)) {
		ActionInfo ai;
		ai.set = set;
		ai.name = info->actionName;
		ai.localized = info->localizedActionName;
		ai.type = info->actionType;
		for (uint32_t i = 0; i < info->countSubactionPaths; i++) {
			ai.subactions.push_back(info->subactionPaths[i]);
		}
		std::lock_guard<std::mutex> g(L().mu);
		inst->actions[*action] = ai;
	}
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrDestroyAction(XrAction action)
{
	InstanceState* inst;
	{
		std::lock_guard<std::mutex> g(L().mu);
		inst = inst_of_action_locked(action);
		if (inst) {
			inst->actions.erase(action);
		}
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	return NEXT(inst).DestroyAction(action);
}

static XRAPI_ATTR XrResult XRAPI_CALL
Visor_xrSuggestInteractionProfileBindings(XrInstance instance, const XrInteractionProfileSuggestedBinding* suggested)
{
	InstanceState* inst;
	{
		std::lock_guard<std::mutex> g(L().mu);
		inst = instance_locked(instance);
	}
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).SuggestInteractionProfileBindings(instance, suggested);
	if (XR_FAILED(r)) {
		return r;
	}
	std::string profile = path_string(inst, suggested->interactionProfile);
	std::vector<std::pair<XrAction, std::string>> list;
	for (uint32_t i = 0; i < suggested->countSuggestedBindings; i++) {
		list.emplace_back(suggested->suggestedBindings[i].action,
		                  path_string(inst, suggested->suggestedBindings[i].binding));
	}
	LOGD("bindings for %s: %u", profile.c_str(), suggested->countSuggestedBindings);
	std::lock_guard<std::mutex> g(L().mu);
	inst->suggested[profile] = std::move(list);
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrAttachSessionActionSets(XrSession session,
                                                                     const XrSessionActionSetsAttachInfo* info)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).AttachSessionActionSets(session, info);
	if (XR_FAILED(r)) {
		return r;
	}
	std::lock_guard<std::mutex> g(L().mu);
	SessionState* s = session_locked(session);
	if (!s) {
		return r;
	}
	for (uint32_t i = 0; i < info->countActionSets; i++) {
		s->attached.push_back(info->actionSets[i]);
	}
	choose_profile_locked(*s);
	queue_profile_changed_locked(*s);
	LOGI("emulating %s (%zu bindings across %zu action sets)",
	     s->profile.empty() ? "no controller profile" : s->profile.c_str(), s->bindings.size(), s->attached.size());
	Json ev = Json::object();
	ev["type"] = "interaction_profile";
	ev["profile"] = s->profile;
	push_event(ev);
	return r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrGetCurrentInteractionProfile(XrSession session, XrPath topLevelUserPath,
                                                                          XrInteractionProfileState* state)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	int hand = hand_of(inst, topLevelUserPath);
	if (!L().drive || hand < 0) {
		return NEXT(inst).GetCurrentInteractionProfile(session, topLevelUserPath, state);
	}
	std::string profile;
	bool attached;
	{
		std::lock_guard<std::mutex> g(L().mu);
		SessionState* s = session_locked(session);
		if (!s) {
			return XR_ERROR_HANDLE_INVALID;
		}
		attached = !s->attached.empty();
		profile = s->profile;
	}
	if (!attached) {
		return XR_ERROR_ACTIONSET_NOT_ATTACHED;
	}
	RigState rig = L().rig.snapshot();
	state->interactionProfile =
	    (!profile.empty() && rig.hands[hand].connected) ? string_path(inst, profile) : XR_NULL_PATH;
	return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrSyncActions(XrSession session, const XrActionsSyncInfo* info)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).SyncActions(session, info);
	if (XR_FAILED(r) || !L().drive) {
		return r;
	}
	RigState rig = L().rig.snapshot();
	std::lock_guard<std::mutex> g(L().mu);
	SessionState* s = session_locked(session);
	if (!s) {
		return r;
	}
	sync_locked(*s, info, rig);
	if (s->synthetic_unfocused) {
		return XR_SESSION_NOT_FOCUSED;
	}
	return r;
}

template <typename T>
static XrResult get_state(XrSession session, const XrActionStateGetInfo* info, T* out, XrActionType want,
                          XrResult (*fill)(const Synced&, T*))
{
	if (!L().drive) {
		return XR_ERROR_RUNTIME_FAILURE; // caller falls through to the runtime
	}
	std::lock_guard<std::mutex> g(L().mu);
	SessionState* s = session_locked(session);
	if (!s || !s->synced_once) {
		return XR_ERROR_RUNTIME_FAILURE;
	}
	auto ait = s->inst->actions.find(info->action);
	if (ait == s->inst->actions.end()) {
		return XR_ERROR_RUNTIME_FAILURE;
	}
	if (ait->second.type != want) {
		return XR_ERROR_ACTION_TYPE_MISMATCH;
	}
	int key = hand_of(s->inst, info->subactionPath);
	if (key == -2) {
		return XR_ERROR_RUNTIME_FAILURE; // a top-level path we don't model
	}
	auto st = s->synced.find({info->action, key});
	if (st == s->synced.end()) {
		Synced none;
		return fill(none, out);
	}
	return fill(st->second, out);
}

static XrResult fill_bool(const Synced& st, XrActionStateBoolean* out)
{
	out->currentState = st.v.b ? XR_TRUE : XR_FALSE;
	out->changedSinceLastSync = st.changed ? XR_TRUE : XR_FALSE;
	out->lastChangeTime = st.last_change;
	out->isActive = st.active ? XR_TRUE : XR_FALSE;
	return XR_SUCCESS;
}

static XrResult fill_float(const Synced& st, XrActionStateFloat* out)
{
	out->currentState = st.v.f;
	out->changedSinceLastSync = st.changed ? XR_TRUE : XR_FALSE;
	out->lastChangeTime = st.last_change;
	out->isActive = st.active ? XR_TRUE : XR_FALSE;
	return XR_SUCCESS;
}

static XrResult fill_vec2(const Synced& st, XrActionStateVector2f* out)
{
	out->currentState = XrVector2f{st.v.x, st.v.y};
	out->changedSinceLastSync = st.changed ? XR_TRUE : XR_FALSE;
	out->lastChangeTime = st.last_change;
	out->isActive = st.active ? XR_TRUE : XR_FALSE;
	return XR_SUCCESS;
}

static XrResult fill_pose(const Synced& st, XrActionStatePose* out)
{
	out->isActive = st.active ? XR_TRUE : XR_FALSE;
	return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrGetActionStateBoolean(XrSession session,
                                                                   const XrActionStateGetInfo* info,
                                                                   XrActionStateBoolean* state)
{
	XrResult r = get_state(session, info, state, XR_ACTION_TYPE_BOOLEAN_INPUT, fill_bool);
	if (r != XR_ERROR_RUNTIME_FAILURE) {
		return r;
	}
	InstanceState* inst = inst_of_session(session);
	return inst ? NEXT(inst).GetActionStateBoolean(session, info, state) : XR_ERROR_HANDLE_INVALID;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrGetActionStateFloat(XrSession session, const XrActionStateGetInfo* info,
                                                                 XrActionStateFloat* state)
{
	XrResult r = get_state(session, info, state, XR_ACTION_TYPE_FLOAT_INPUT, fill_float);
	if (r != XR_ERROR_RUNTIME_FAILURE) {
		return r;
	}
	InstanceState* inst = inst_of_session(session);
	return inst ? NEXT(inst).GetActionStateFloat(session, info, state) : XR_ERROR_HANDLE_INVALID;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrGetActionStateVector2f(XrSession session,
                                                                    const XrActionStateGetInfo* info,
                                                                    XrActionStateVector2f* state)
{
	XrResult r = get_state(session, info, state, XR_ACTION_TYPE_VECTOR2F_INPUT, fill_vec2);
	if (r != XR_ERROR_RUNTIME_FAILURE) {
		return r;
	}
	InstanceState* inst = inst_of_session(session);
	return inst ? NEXT(inst).GetActionStateVector2f(session, info, state) : XR_ERROR_HANDLE_INVALID;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrGetActionStatePose(XrSession session, const XrActionStateGetInfo* info,
                                                                XrActionStatePose* state)
{
	XrResult r = get_state(session, info, state, XR_ACTION_TYPE_POSE_INPUT, fill_pose);
	if (r != XR_ERROR_RUNTIME_FAILURE) {
		return r;
	}
	InstanceState* inst = inst_of_session(session);
	return inst ? NEXT(inst).GetActionStatePose(session, info, state) : XR_ERROR_HANDLE_INVALID;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrEnumerateBoundSourcesForAction(
    XrSession session, const XrBoundSourcesForActionEnumerateInfo* info, uint32_t capacity, uint32_t* count,
    XrPath* sources)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	if (!L().drive) {
		return NEXT(inst).EnumerateBoundSourcesForAction(session, info, capacity, count, sources);
	}
	std::vector<std::string> paths;
	{
		std::lock_guard<std::mutex> g(L().mu);
		SessionState* s = session_locked(session);
		if (!s) {
			return XR_ERROR_HANDLE_INVALID;
		}
		if (s->attached.empty()) {
			return XR_ERROR_ACTIONSET_NOT_ATTACHED;
		}
		for (const auto& b : s->bindings) {
			if (b.action == info->action) {
				paths.push_back(b.source.path);
			}
		}
	}
	*count = (uint32_t)paths.size();
	if (capacity == 0) {
		return XR_SUCCESS;
	}
	if (capacity < paths.size()) {
		return XR_ERROR_SIZE_INSUFFICIENT;
	}
	for (size_t i = 0; i < paths.size(); i++) {
		sources[i] = string_path(inst, paths[i]);
	}
	return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrGetInputSourceLocalizedName(XrSession session,
                                                                         const XrInputSourceLocalizedNameGetInfo* info,
                                                                         uint32_t capacity, uint32_t* count,
                                                                         char* buffer)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	if (!L().drive) {
		return NEXT(inst).GetInputSourceLocalizedName(session, info, capacity, count, buffer);
	}
	// The sources are ours (the runtime never bound them), so we name them.
	std::string path = path_string(inst, info->sourcePath);
	std::string name;
	if (info->whichComponents & XR_INPUT_SOURCE_LOCALIZED_NAME_USER_PATH_BIT) {
		name += path.find("/left") != std::string::npos ? "Left Hand " : "Right Hand ";
	}
	if (info->whichComponents & XR_INPUT_SOURCE_LOCALIZED_NAME_INTERACTION_PROFILE_BIT) {
		name += "Touch Controller ";
	}
	if (info->whichComponents & XR_INPUT_SOURCE_LOCALIZED_NAME_COMPONENT_BIT) {
		size_t k = path.find("/input/");
		name += k != std::string::npos ? path.substr(k + 7) : path;
	}
	*count = (uint32_t)name.size() + 1;
	if (capacity == 0) {
		return XR_SUCCESS;
	}
	if (capacity < *count) {
		return XR_ERROR_SIZE_INSUFFICIENT;
	}
	std::memcpy(buffer, name.c_str(), name.size() + 1);
	return XR_SUCCESS;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrApplyHapticFeedback(XrSession session,
                                                                 const XrHapticActionInfo* info,
                                                                 const XrHapticBaseHeader* haptic)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	std::string action_name;
	{
		std::lock_guard<std::mutex> g(L().mu);
		auto it = inst->actions.find(info->action);
		if (it != inst->actions.end()) {
			action_name = it->second.name;
		}
	}
	Json ev = Json::object();
	ev["type"] = "haptic";
	ev["action"] = action_name;
	int hand = hand_of(inst, info->subactionPath);
	ev["hand"] = hand == kLeft ? "left" : (hand == kRight ? "right" : "both");
	if (haptic && haptic->type == XR_TYPE_HAPTIC_VIBRATION) {
		auto* v = reinterpret_cast<const XrHapticVibration*>(haptic);
		ev["amplitude"] = v->amplitude;
		ev["duration_ms"] = v->duration > 0 ? (double)v->duration / 1e6 : -1.0;
		ev["frequency"] = v->frequency;
	}
	push_event(ev);
	XrResult r = NEXT(inst).ApplyHapticFeedback(session, info, haptic);
	return L().drive ? XR_SUCCESS : r;
}

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrStopHapticFeedback(XrSession session, const XrHapticActionInfo* info)
{
	InstanceState* inst = inst_of_session(session);
	if (!inst) {
		return XR_ERROR_HANDLE_INVALID;
	}
	XrResult r = NEXT(inst).StopHapticFeedback(session, info);
	return L().drive ? XR_SUCCESS : r;
}

// ---------------------------------------------------------------------------
// Negotiation and proc address lookup

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrCreateApiLayerInstance(const XrInstanceCreateInfo* info,
                                                                    const XrApiLayerCreateInfo* layerInfo,
                                                                    XrInstance* instance)
{
	if (!layerInfo || !layerInfo->nextInfo) {
		return XR_ERROR_INITIALIZATION_FAILED;
	}
	static bool configured = false;
	if (!configured) {
		read_config();
		configured = true;
	}
	XrApiLayerCreateInfo next_info = *layerInfo;
	next_info.nextInfo = layerInfo->nextInfo->next;
	PFN_xrGetInstanceProcAddr gipa = layerInfo->nextInfo->nextGetInstanceProcAddr;

	// We use XR_KHR_convert_timespec_time to line up runtime-captured frames
	// with the app's frames; enable it quietly if the runtime has it.
	const char* kTimespec = XR_KHR_CONVERT_TIMESPEC_TIME_EXTENSION_NAME;
	bool app_has_timespec = false;
	for (uint32_t i = 0; i < info->enabledExtensionCount; i++) {
		app_has_timespec = app_has_timespec || std::strcmp(info->enabledExtensionNames[i], kTimespec) == 0;
	}
	bool runtime_has_timespec = false;
	PFN_xrEnumerateInstanceExtensionProperties enum_ext = nullptr;
	if (XR_SUCCEEDED(gipa(XR_NULL_HANDLE, "xrEnumerateInstanceExtensionProperties",
	                      reinterpret_cast<PFN_xrVoidFunction*>(&enum_ext))) &&
	    enum_ext) {
		uint32_t n = 0;
		if (XR_SUCCEEDED(enum_ext(nullptr, 0, &n, nullptr)) && n > 0) {
			std::vector<XrExtensionProperties> props(n, XrExtensionProperties{XR_TYPE_EXTENSION_PROPERTIES});
			if (XR_SUCCEEDED(enum_ext(nullptr, n, &n, props.data()))) {
				for (const auto& e : props) {
					runtime_has_timespec = runtime_has_timespec || std::strcmp(e.extensionName, kTimespec) == 0;
				}
			}
		}
	}
	XrInstanceCreateInfo ci = *info;
	std::vector<const char*> exts(info->enabledExtensionNames, info->enabledExtensionNames + info->enabledExtensionCount);
	if (runtime_has_timespec && !app_has_timespec) {
		exts.push_back(kTimespec);
		ci.enabledExtensionCount = (uint32_t)exts.size();
		ci.enabledExtensionNames = exts.data();
	}
	XrResult r = layerInfo->nextInfo->nextCreateApiLayerInstance(&ci, &next_info, instance);
	if (XR_FAILED(r) && ci.enabledExtensionCount != info->enabledExtensionCount) {
		r = layerInfo->nextInfo->nextCreateApiLayerInstance(info, &next_info, instance);
		runtime_has_timespec = false;
	}
	if (XR_FAILED(r)) {
		return r;
	}

	auto inst = std::make_unique<InstanceState>();
	inst->handle = *instance;
	inst->next.GetInstanceProcAddr = gipa;
#define LOAD_REQ(name) gipa(*instance, "xr" #name, reinterpret_cast<PFN_xrVoidFunction*>(&inst->next.name));
	VISOR_REQUIRED_FUNCS(LOAD_REQ)
	VISOR_OPTIONAL_FUNCS(LOAD_REQ)
#undef LOAD_REQ
	inst->app_name = info->applicationInfo.applicationName;
	inst->engine_name = info->applicationInfo.engineName;
	inst->app_version = info->applicationInfo.applicationVersion;
	inst->engine_version = info->applicationInfo.engineVersion;
	inst->api_version = info->applicationInfo.apiVersion;
	for (uint32_t i = 0; i < info->enabledExtensionCount; i++) {
		inst->extensions.emplace_back(info->enabledExtensionNames[i]);
	}
	XrInstanceProperties props{XR_TYPE_INSTANCE_PROPERTIES};
	if (inst->next.GetInstanceProperties && XR_SUCCEEDED(inst->next.GetInstanceProperties(*instance, &props))) {
		inst->runtime_name = props.runtimeName;
	}
	if (runtime_has_timespec || app_has_timespec) {
		gipa(*instance, "xrConvertTimespecTimeToTimeKHR", reinterpret_cast<PFN_xrVoidFunction*>(&inst->convert_timespec));
	}
	inst->next.StringToPath(*instance, "/user/hand/left", &inst->left);
	inst->next.StringToPath(*instance, "/user/hand/right", &inst->right);

	LOGI("%s %s loaded into '%s' (%s) on %s, %s", VISOR_LAYER_NAME, VISOR_LAYER_VERSION, inst->app_name.c_str(),
	     inst->engine_name.empty() ? "no engine name" : inst->engine_name.c_str(), inst->runtime_name.c_str(),
	     L().drive ? "driving input" : "observing only");
	{
		std::lock_guard<std::mutex> g(L().mu);
		L().instances[*instance] = std::move(inst);
	}
	ensure_control_server();
	return r;
}

struct Intercept
{
	const char* name;
	PFN_xrVoidFunction fn;
	bool optional_next; // only offer it if the next layer has it
};

#define FN(name) reinterpret_cast<PFN_xrVoidFunction>(Visor_##name)
static const Intercept kIntercepts[] = {
    {"xrGetInstanceProcAddr", FN(xrGetInstanceProcAddr), false},
    {"xrDestroyInstance", FN(xrDestroyInstance), false},
    {"xrGetSystemProperties", FN(xrGetSystemProperties), false},
    {"xrPollEvent", FN(xrPollEvent), false},
    {"xrCreateSession", FN(xrCreateSession), false},
    {"xrDestroySession", FN(xrDestroySession), false},
    {"xrBeginSession", FN(xrBeginSession), false},
    {"xrEndSession", FN(xrEndSession), false},
    {"xrCreateReferenceSpace", FN(xrCreateReferenceSpace), false},
    {"xrCreateActionSpace", FN(xrCreateActionSpace), false},
    {"xrDestroySpace", FN(xrDestroySpace), false},
    {"xrLocateSpace", FN(xrLocateSpace), false},
    {"xrLocateSpaces", FN(xrLocateSpaces), true},
    {"xrLocateSpacesKHR", FN(xrLocateSpacesKHR), true},
    {"xrLocateViews", FN(xrLocateViews), false},
    {"xrCreateSwapchain", FN(xrCreateSwapchain), false},
    {"xrDestroySwapchain", FN(xrDestroySwapchain), false},
    {"xrEnumerateSwapchainImages", FN(xrEnumerateSwapchainImages), false},
    {"xrAcquireSwapchainImage", FN(xrAcquireSwapchainImage), false},
    {"xrReleaseSwapchainImage", FN(xrReleaseSwapchainImage), false},
    {"xrWaitFrame", FN(xrWaitFrame), false},
    {"xrBeginFrame", FN(xrBeginFrame), false},
    {"xrEndFrame", FN(xrEndFrame), false},
    {"xrCreateActionSet", FN(xrCreateActionSet), false},
    {"xrDestroyActionSet", FN(xrDestroyActionSet), false},
    {"xrCreateAction", FN(xrCreateAction), false},
    {"xrDestroyAction", FN(xrDestroyAction), false},
    {"xrSuggestInteractionProfileBindings", FN(xrSuggestInteractionProfileBindings), false},
    {"xrAttachSessionActionSets", FN(xrAttachSessionActionSets), false},
    {"xrGetCurrentInteractionProfile", FN(xrGetCurrentInteractionProfile), false},
    {"xrSyncActions", FN(xrSyncActions), false},
    {"xrGetActionStateBoolean", FN(xrGetActionStateBoolean), false},
    {"xrGetActionStateFloat", FN(xrGetActionStateFloat), false},
    {"xrGetActionStateVector2f", FN(xrGetActionStateVector2f), false},
    {"xrGetActionStatePose", FN(xrGetActionStatePose), false},
    {"xrEnumerateBoundSourcesForAction", FN(xrEnumerateBoundSourcesForAction), false},
    {"xrGetInputSourceLocalizedName", FN(xrGetInputSourceLocalizedName), false},
    {"xrApplyHapticFeedback", FN(xrApplyHapticFeedback), false},
    {"xrStopHapticFeedback", FN(xrStopHapticFeedback), false},
};
#undef FN

static XRAPI_ATTR XrResult XRAPI_CALL Visor_xrGetInstanceProcAddr(XrInstance instance, const char* name,
                                                                 PFN_xrVoidFunction* function)
{
	InstanceState* inst = nullptr;
	{
		std::lock_guard<std::mutex> g(L().mu);
		inst = instance_locked(instance);
	}
	if (inst) {
		for (const auto& ic : kIntercepts) {
			if (std::strcmp(ic.name, name) != 0) {
				continue;
			}
			if (ic.optional_next) {
				PFN_xrVoidFunction probe = nullptr;
				if (XR_FAILED(inst->next.GetInstanceProcAddr(instance, name, &probe)) || !probe) {
					break; // let the runtime report it unsupported
				}
			}
			*function = ic.fn;
			return XR_SUCCESS;
		}
		return inst->next.GetInstanceProcAddr(instance, name, function);
	}
	// No instance yet (or not ours): only the proc-addr lookup itself.
	if (std::strcmp(name, "xrGetInstanceProcAddr") == 0) {
		*function = reinterpret_cast<PFN_xrVoidFunction>(Visor_xrGetInstanceProcAddr);
		return XR_SUCCESS;
	}
	*function = nullptr;
	return XR_ERROR_HANDLE_INVALID;
}

} // namespace visor

extern "C" {

__attribute__((visibility("default"))) XRAPI_ATTR XrResult XRAPI_CALL xrNegotiateLoaderApiLayerInterface(const XrNegotiateLoaderInfo* loaderInfo,
                                                                  const char* layerName,
                                                                  XrNegotiateApiLayerRequest* request)
{
	(void)layerName;
	if (!loaderInfo || !request || loaderInfo->structType != XR_LOADER_INTERFACE_STRUCT_LOADER_INFO ||
	    request->structType != XR_LOADER_INTERFACE_STRUCT_API_LAYER_REQUEST) {
		return XR_ERROR_INITIALIZATION_FAILED;
	}
	if (loaderInfo->minInterfaceVersion > XR_CURRENT_LOADER_API_LAYER_VERSION ||
	    loaderInfo->maxInterfaceVersion < XR_CURRENT_LOADER_API_LAYER_VERSION) {
		return XR_ERROR_INITIALIZATION_FAILED;
	}
	// The loader dlcloses layers after the last instance; keep our code mapped
	// for any thread still finishing a request.
	Dl_info self{};
	if (dladdr(reinterpret_cast<void*>(&xrNegotiateLoaderApiLayerInterface), &self) && self.dli_fname) {
		dlopen(self.dli_fname, RTLD_NOW | RTLD_NOLOAD | RTLD_NODELETE);
	}
	request->layerInterfaceVersion = XR_CURRENT_LOADER_API_LAYER_VERSION;
	request->layerApiVersion = XR_CURRENT_API_VERSION;
	request->getInstanceProcAddr = visor::Visor_xrGetInstanceProcAddr;
	request->createApiLayerInstance = visor::Visor_xrCreateApiLayerInstance;
	return XR_SUCCESS;
}

} // extern "C"
