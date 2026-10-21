// Frame capture: the layer's "eyes".
//
// When the app releases a projection swapchain image, the layer records a blit
// of the eye rectangles into its own small RGBA8 image, on the app's graphics
// queue, before the runtime takes the image back. At xrEndFrame a copy to a
// host buffer is submitted with a fence; a worker thread waits on it and
// publishes the pixels to a POSIX shared-memory ring that `visor` maps.
//
// Shared memory layout (little endian), see visor/xr/frames.py:
//   0     char[8]  magic "VISORXR1"
//   8     u32      version (1)
//   12    u32      slot_count
//   16    u64      slot_bytes
//   24    u64      latest_seq        (0 = nothing yet)
//   32    u32      latest_slot
//   36    u32      reserved
//   64 + 64*i      slot header: u64 seq, u32 width, u32 height, u32 stride,
//                  u32 eyes, i64 frame, i64 display_time, i64 capture_ns,
//                  u32 format (0 = RGBA8, sRGB-encoded), u32 reserved
//   4096 + i*slot_bytes  pixel rows, top-down
// A slot's seq is zeroed while it is being written (seqlock).
#pragma once

#include "common.h"

#include <atomic>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace visor {

struct CaptureView
{
	int eye;                // 0 left, 1 right
	uint32_t array_index;   // imageArrayIndex
	XrRect2Di rect;         // imageRect inside the swapchain image
};

struct CaptureInfo
{
	uint64_t seq = 0;
	uint32_t width = 0, height = 0, eyes = 0;
	int64_t frame = 0;        // the app's frame index (-1 when unknown)
	int64_t display_time = 0; // XrTime (layer capture) or monotonic ns (runtime)
	int64_t capture_ns = 0;
};

// Where frames come from. The virtual headset (Monado with Visor's patch)
// copies frames compositor-side, which never touches the app's GPU queue;
// on other runtimes the layer can copy them itself (Capturer).
class FrameSource
{
public:
	virtual ~FrameSource() = default;
	virtual bool ok() const = 0;
	virtual const std::string& error() const = 0;
	virtual const std::string& shm_name() const = 0;
	virtual const char* kind() const = 0;

	virtual void configure(uint32_t eye_width, bool both_eyes, double fps) = 0;
	virtual uint32_t eye_width() const = 0;
	virtual bool both_eyes() const = 0;
	virtual double fps() const = 0;
	virtual void set_every_frame(bool on) = 0;

	// Ask for a frame taken after this call; wait() returns once one exists.
	virtual uint64_t request() = 0;
	virtual bool wait(uint64_t ticket, int timeout_ms, CaptureInfo* out) = 0;
	virtual CaptureInfo latest() const = 0;
	virtual void shutdown() = 0;

	// Hooks for sources that copy on the app's queue.
	virtual void on_release(VkImage, VkFormat, uint32_t, uint32_t, const std::vector<CaptureView>&, int64_t) {}
	virtual void on_end_frame(int64_t, int64_t) {}
};

// Reads frames the runtime publishes into shared memory (see
// native/monado: null_visor_capture.c for the layout).
class RuntimeFrames : public FrameSource
{
public:
	explicit RuntimeFrames(std::string shm_name);
	~RuntimeFrames() override;

	bool ok() const override { return shm_ != nullptr; }
	const std::string& error() const override { return error_; }
	const std::string& shm_name() const override { return name_; }
	const char* kind() const override { return "runtime"; }

	void configure(uint32_t eye_width, bool both_eyes, double fps) override;
	uint32_t eye_width() const override;
	bool both_eyes() const override;
	double fps() const override { return fps_; }
	void set_every_frame(bool on) override;

	uint64_t request() override;
	bool wait(uint64_t ticket, int timeout_ms, CaptureInfo* out) override;
	CaptureInfo latest() const override;
	void shutdown() override;

private:
	void write_fps(double fps);

	std::string name_, error_;
	uint8_t* shm_ = nullptr;
	size_t size_ = 0;
	double fps_ = 0.0;
	bool every_frame_ = false;
};

class Capturer : public FrameSource
{
public:
	Capturer(const XrGraphicsBindingVulkanKHR& binding, std::string shm_name);
	~Capturer() override;

	bool ok() const override { return ok_; }
	const std::string& error() const override { return error_; }
	const std::string& shm_name() const override { return shm_name_; }
	const char* kind() const override { return "layer"; }

	// width per eye (height follows the eye's aspect), which eyes, and how
	// often to capture without being asked (0 = only on request).
	void configure(uint32_t eye_width, bool both_eyes, double fps) override;
	uint32_t eye_width() const override { return eye_width_; }
	bool both_eyes() const override { return both_eyes_; }
	double fps() const override { return fps_; }

	// Capture every frame regardless of fps (used while single-stepping).
	void set_every_frame(bool on) override { every_frame_ = on; }

	// Hooks, called with the app's queue externally synchronized.
	void on_release(VkImage image, VkFormat format, uint32_t img_w, uint32_t img_h,
	                const std::vector<CaptureView>& views, int64_t frame) override;
	void on_end_frame(int64_t frame, int64_t display_time) override;

	// Ask for a capture; returns the seq that a fresh frame will exceed.
	uint64_t request() override;
	// Wait until a frame with seq > after is published.
	bool wait(uint64_t after, int timeout_ms, CaptureInfo* out) override;
	CaptureInfo latest() const override;

	// Must be called before the VkDevice goes away.
	void shutdown() override;

private:
	struct Slot
	{
		VkImage image = VK_NULL_HANDLE;
		VkDeviceMemory image_mem = VK_NULL_HANDLE;
		VkBuffer buffer = VK_NULL_HANDLE;
		VkDeviceMemory buffer_mem = VK_NULL_HANDLE;
		void* mapped = nullptr;
		VkFence fence = VK_NULL_HANDLE;
		std::vector<VkCommandBuffer> cmds; // [0] end-of-frame copy, rest blits
		size_t next_cmd = 1;
		uint32_t width = 0, height = 0; // whole staging image (eyes side by side)
		uint32_t eye_w = 0;
		VkFormat format = VK_FORMAT_UNDEFINED;
		bool in_flight = false;  // fenced copy submitted, worker not done
		bool initialized = false; // staging image transitioned this frame
		int64_t frame = -1;
		int64_t display_time = 0;
		uint32_t eyes = 0;
	};

	bool load_vulkan();
	bool create_shm(size_t slot_bytes);
	bool ensure_slot(Slot& s, uint32_t w, uint32_t h, uint32_t eye_w, VkFormat fmt);
	void destroy_slot(Slot& s);
	VkCommandBuffer next_blit_cmd(Slot& s);
	uint32_t memory_type(uint32_t bits, VkMemoryPropertyFlags props);
	void worker();

	bool ok_ = false;
	std::string error_;
	XrGraphicsBindingVulkanKHR binding_{};
	std::string shm_name_;

	// Vulkan entry points (device level unless noted).
	PFN_vkGetInstanceProcAddr gipa_ = nullptr;
	PFN_vkGetDeviceProcAddr gdpa_ = nullptr;
	PFN_vkGetPhysicalDeviceMemoryProperties GetPhysicalDeviceMemoryProperties = nullptr;
	PFN_vkGetPhysicalDeviceFormatProperties GetPhysicalDeviceFormatProperties = nullptr;
	PFN_vkGetDeviceQueue GetDeviceQueue = nullptr;
	PFN_vkCreateCommandPool CreateCommandPool = nullptr;
	PFN_vkDestroyCommandPool DestroyCommandPool = nullptr;
	PFN_vkAllocateCommandBuffers AllocateCommandBuffers = nullptr;
	PFN_vkResetCommandBuffer ResetCommandBuffer = nullptr;
	PFN_vkBeginCommandBuffer BeginCommandBuffer = nullptr;
	PFN_vkEndCommandBuffer EndCommandBuffer = nullptr;
	PFN_vkCmdPipelineBarrier CmdPipelineBarrier = nullptr;
	PFN_vkCmdBlitImage CmdBlitImage = nullptr;
	PFN_vkCmdCopyImageToBuffer CmdCopyImageToBuffer = nullptr;
	PFN_vkQueueSubmit QueueSubmit = nullptr;
	PFN_vkCreateFence CreateFence = nullptr;
	PFN_vkDestroyFence DestroyFence = nullptr;
	PFN_vkWaitForFences WaitForFences = nullptr;
	PFN_vkResetFences ResetFences = nullptr;
	PFN_vkCreateImage CreateImage = nullptr;
	PFN_vkDestroyImage DestroyImage = nullptr;
	PFN_vkCreateBuffer CreateBuffer = nullptr;
	PFN_vkDestroyBuffer DestroyBuffer = nullptr;
	PFN_vkGetImageMemoryRequirements GetImageMemoryRequirements = nullptr;
	PFN_vkGetBufferMemoryRequirements GetBufferMemoryRequirements = nullptr;
	PFN_vkAllocateMemory AllocateMemory = nullptr;
	PFN_vkFreeMemory FreeMemory = nullptr;
	PFN_vkBindImageMemory BindImageMemory = nullptr;
	PFN_vkBindBufferMemory BindBufferMemory = nullptr;
	PFN_vkMapMemory MapMemory = nullptr;
	PFN_vkUnmapMemory UnmapMemory = nullptr;

	VkQueue queue_ = VK_NULL_HANDLE;
	VkCommandPool pool_ = VK_NULL_HANDLE;
	VkPhysicalDeviceMemoryProperties mem_props_{};

	static constexpr int kSlots = 3;
	Slot slots_[kSlots];
	int active_ = -1;          // slot being filled this frame
	int64_t active_frame_ = -1;
	int64_t decided_frame_ = -1;
	int64_t last_capture_ns_ = 0;

	std::atomic<uint32_t> eye_width_{960};
	std::atomic<bool> both_eyes_{false};
	std::atomic<double> fps_{0.0};
	std::atomic<bool> every_frame_{false};
	std::atomic<uint32_t> requests_{0};
	bool warned_format_ = false;

	// Shared memory.
	int shm_fd_ = -1;
	uint8_t* shm_ = nullptr;
	size_t shm_size_ = 0;
	size_t slot_bytes_ = 0;

	// Worker + publication.
	mutable std::mutex mu_;
	std::condition_variable cv_;      // worker wakeups
	std::condition_variable pub_cv_;  // published frames
	std::deque<int> fenced_;          // slots awaiting their fence
	std::atomic<bool> stop_{false};
	std::thread thread_;
	uint64_t seq_ = 0;
	CaptureInfo latest_;
	uint32_t shm_slot_ = 0;
};

} // namespace visor
