// Visor agent layer — shared includes, logging and pose math.
//
// The layer sits between an OpenXR app and whatever runtime it would normally
// talk to. Everything here is header-only and dependency-free on purpose: the
// layer gets loaded into arbitrary engines, so it must not drag in libraries
// that could clash with the app's own.
#pragma once

#include <time.h>
#include <vulkan/vulkan.h>

#define XR_USE_GRAPHICS_API_VULKAN 1
#define XR_USE_TIMESPEC 1
#include <openxr/openxr.h>
#include <openxr/openxr_loader_negotiation.h>
#include <openxr/openxr_platform.h>

#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <string>

#define VISOR_LAYER_NAME "XR_APILAYER_VISOR_agent"
#define VISOR_LAYER_VERSION "0.2.0"

namespace visor {

// ---------------------------------------------------------------------------
// Logging. VISOR_XR_LOG=error|warn|info|debug (default info). Goes to stderr,
// which is where engines expect native noise and where `visor sim` collects it.

enum class Level { Error = 0, Warn = 1, Info = 2, Debug = 3 };

Level log_level();
void log(Level level, const char* fmt, ...) __attribute__((format(printf, 2, 3)));

#define LOGE(...) ::visor::log(::visor::Level::Error, __VA_ARGS__)
#define LOGW(...) ::visor::log(::visor::Level::Warn, __VA_ARGS__)
#define LOGI(...) ::visor::log(::visor::Level::Info, __VA_ARGS__)
#define LOGD(...)                                                                   \
	do {                                                                            \
		if (::visor::log_level() >= ::visor::Level::Debug)                          \
			::visor::log(::visor::Level::Debug, __VA_ARGS__);                       \
	} while (0)

// Monotonic nanoseconds, same clock family the runtimes use for XrTime.
int64_t now_ns();

// ---------------------------------------------------------------------------
// Pose math. OpenXR convention: right-handed, +Y up, -Z forward, metres.

inline XrVector3f v3(float x, float y, float z) { return XrVector3f{x, y, z}; }
inline XrVector3f operator+(XrVector3f a, XrVector3f b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
inline XrVector3f operator-(XrVector3f a, XrVector3f b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
inline XrVector3f operator*(XrVector3f a, float s) { return {a.x * s, a.y * s, a.z * s}; }
inline float dot(XrVector3f a, XrVector3f b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline XrVector3f cross(XrVector3f a, XrVector3f b)
{
	return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x};
}
inline float length(XrVector3f a) { return std::sqrt(dot(a, a)); }

inline XrQuaternionf quat_identity() { return XrQuaternionf{0, 0, 0, 1}; }

inline XrQuaternionf operator*(XrQuaternionf a, XrQuaternionf b)
{
	return {
	    a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y,
	    a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
	    a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w,
	    a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z,
	};
}

inline XrQuaternionf conj(XrQuaternionf q) { return {-q.x, -q.y, -q.z, q.w}; }

inline XrQuaternionf normalize(XrQuaternionf q)
{
	float n = std::sqrt(q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w);
	if (n < 1e-8f) {
		return quat_identity();
	}
	return {q.x / n, q.y / n, q.z / n, q.w / n};
}

inline XrVector3f rotate(XrQuaternionf q, XrVector3f v)
{
	// v' = v + 2w(u x v) + 2u x (u x v), u = q.xyz
	XrVector3f u{q.x, q.y, q.z};
	XrVector3f t = cross(u, v) * 2.0f;
	return v + t * q.w + cross(u, t);
}

inline XrQuaternionf axis_angle(XrVector3f axis, float radians)
{
	float s = std::sin(radians * 0.5f);
	float n = length(axis);
	if (n < 1e-8f) {
		return quat_identity();
	}
	return {axis.x / n * s, axis.y / n * s, axis.z / n * s, std::cos(radians * 0.5f)};
}

constexpr float kDegToRad = 3.14159265358979323846f / 180.0f;

// Yaw turns left (about +Y), pitch looks up (about +X), roll tilts the head
// towards the left shoulder (about +Z). Degrees, applied yaw-pitch-roll.
inline XrQuaternionf from_ypr(float yaw_deg, float pitch_deg, float roll_deg)
{
	XrQuaternionf y = axis_angle({0, 1, 0}, yaw_deg * kDegToRad);
	XrQuaternionf p = axis_angle({1, 0, 0}, pitch_deg * kDegToRad);
	XrQuaternionf r = axis_angle({0, 0, 1}, roll_deg * kDegToRad);
	return normalize(y * p * r);
}

inline XrPosef pose_identity() { return XrPosef{quat_identity(), {0, 0, 0}}; }

// a * b: b expressed in a's frame, result in a's parent frame.
inline XrPosef compose(const XrPosef& a, const XrPosef& b)
{
	return XrPosef{normalize(a.orientation * b.orientation), a.position + rotate(a.orientation, b.position)};
}

inline XrPosef inverse(const XrPosef& a)
{
	XrQuaternionf qi = conj(a.orientation);
	return XrPosef{qi, rotate(qi, a.position * -1.0f)};
}

// Pose of `child` (in world) expressed in `base`'s frame (in world).
inline XrPosef relative(const XrPosef& base, const XrPosef& child) { return compose(inverse(base), child); }

inline XrQuaternionf slerp(XrQuaternionf a, XrQuaternionf b, float t)
{
	float d = a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w;
	if (d < 0) {
		b = {-b.x, -b.y, -b.z, -b.w};
		d = -d;
	}
	if (d > 0.9995f) {
		return normalize({a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t,
		                  a.w + (b.w - a.w) * t});
	}
	float th = std::acos(d);
	float s = std::sin(th);
	float wa = std::sin((1 - t) * th) / s;
	float wb = std::sin(t * th) / s;
	return {a.x * wa + b.x * wb, a.y * wa + b.y * wb, a.z * wa + b.z * wb, a.w * wa + b.w * wb};
}

inline XrPosef lerp_pose(const XrPosef& a, const XrPosef& b, float t)
{
	return XrPosef{slerp(a.orientation, b.orientation, t), a.position + (b.position - a.position) * t};
}

// Rotation (radians) between two orientations, for comfort metrics.
inline float angle_between(XrQuaternionf a, XrQuaternionf b)
{
	float d = std::fabs(a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w);
	if (d > 1.0f) {
		d = 1.0f;
	}
	return 2.0f * std::acos(d);
}

} // namespace visor
