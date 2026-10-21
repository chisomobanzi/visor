#include "capture.h"

#include <dlfcn.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <chrono>
#include <cstring>

namespace visor {

namespace {

constexpr uint32_t kMaxEyeWidth = 2048;
constexpr uint32_t kMaxWidth = kMaxEyeWidth * 2;
constexpr uint32_t kMaxHeight = 2400;
constexpr size_t kHeaderBytes = 4096;

bool srgb_like(VkFormat f)
{
	switch (f) {
	case VK_FORMAT_R8G8B8A8_SRGB:
	case VK_FORMAT_B8G8R8A8_SRGB:
	case VK_FORMAT_A8B8G8R8_SRGB_PACK32:
	case VK_FORMAT_R8G8B8_SRGB:
	case VK_FORMAT_B8G8R8_SRGB:
	// Float targets hold linear light; encode them for display too.
	case VK_FORMAT_R16G16B16A16_SFLOAT:
	case VK_FORMAT_R32G32B32A32_SFLOAT:
	case VK_FORMAT_B10G11R11_UFLOAT_PACK32: return true;
	default: return false;
	}
}

template <typename T> void store_release(uint8_t* base, size_t off, T v)
{
	__atomic_store_n(reinterpret_cast<T*>(base + off), v, __ATOMIC_RELEASE);
}

template <typename T> void put(uint8_t* base, size_t off, T v) { std::memcpy(base + off, &v, sizeof(T)); }

VkImageMemoryBarrier image_barrier(VkImage image, VkImageLayout from, VkImageLayout to, VkAccessFlags src,
                                   VkAccessFlags dst)
{
	VkImageMemoryBarrier b{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
	b.srcAccessMask = src;
	b.dstAccessMask = dst;
	b.oldLayout = from;
	b.newLayout = to;
	b.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
	b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
	b.image = image;
	b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, VK_REMAINING_ARRAY_LAYERS};
	return b;
}

} // namespace

Capturer::Capturer(const XrGraphicsBindingVulkanKHR& binding, std::string shm_name)
    : binding_(binding), shm_name_(std::move(shm_name))
{
	if (!load_vulkan()) {
		return;
	}
	GetPhysicalDeviceMemoryProperties(binding_.physicalDevice, &mem_props_);
	GetDeviceQueue(binding_.device, binding_.queueFamilyIndex, binding_.queueIndex, &queue_);

	VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
	pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
	pci.queueFamilyIndex = binding_.queueFamilyIndex;
	if (CreateCommandPool(binding_.device, &pci, nullptr, &pool_) != VK_SUCCESS) {
		error_ = "vkCreateCommandPool failed";
		return;
	}
	slot_bytes_ = (size_t)kMaxWidth * kMaxHeight * 4;
	if (!create_shm(slot_bytes_)) {
		return;
	}
	ok_ = true;
	thread_ = std::thread([this] { worker(); });
	LOGI("frame capture ready (shm %s)", shm_name_.c_str());
}

Capturer::~Capturer() { shutdown(); }

bool Capturer::load_vulkan()
{
	void* lib = dlopen("libvulkan.so.1", RTLD_NOW | RTLD_LOCAL);
	if (!lib) {
		error_ = "libvulkan.so.1 not loadable";
		return false;
	}
	gipa_ = (PFN_vkGetInstanceProcAddr)dlsym(lib, "vkGetInstanceProcAddr");
	if (!gipa_) {
		error_ = "vkGetInstanceProcAddr missing";
		return false;
	}
	VkInstance inst = binding_.instance;
	gdpa_ = (PFN_vkGetDeviceProcAddr)gipa_(inst, "vkGetDeviceProcAddr");
	GetPhysicalDeviceMemoryProperties =
	    (PFN_vkGetPhysicalDeviceMemoryProperties)gipa_(inst, "vkGetPhysicalDeviceMemoryProperties");
	GetPhysicalDeviceFormatProperties =
	    (PFN_vkGetPhysicalDeviceFormatProperties)gipa_(inst, "vkGetPhysicalDeviceFormatProperties");
	if (!gdpa_ || !GetPhysicalDeviceMemoryProperties || !GetPhysicalDeviceFormatProperties) {
		error_ = "instance-level Vulkan functions missing";
		return false;
	}
	VkDevice dev = binding_.device;
	bool all = true;
#define LOAD(name)                                                                                          \
	name = (PFN_vk##name)gdpa_(dev, "vk" #name);                                                            \
	all = all && name != nullptr;
	LOAD(GetDeviceQueue)
	LOAD(CreateCommandPool)
	LOAD(DestroyCommandPool)
	LOAD(AllocateCommandBuffers)
	LOAD(ResetCommandBuffer)
	LOAD(BeginCommandBuffer)
	LOAD(EndCommandBuffer)
	LOAD(CmdPipelineBarrier)
	LOAD(CmdBlitImage)
	LOAD(CmdCopyImageToBuffer)
	LOAD(QueueSubmit)
	LOAD(CreateFence)
	LOAD(DestroyFence)
	LOAD(WaitForFences)
	LOAD(ResetFences)
	LOAD(CreateImage)
	LOAD(DestroyImage)
	LOAD(CreateBuffer)
	LOAD(DestroyBuffer)
	LOAD(GetImageMemoryRequirements)
	LOAD(GetBufferMemoryRequirements)
	LOAD(AllocateMemory)
	LOAD(FreeMemory)
	LOAD(BindImageMemory)
	LOAD(BindBufferMemory)
	LOAD(MapMemory)
	LOAD(UnmapMemory)
#undef LOAD
	if (!all) {
		error_ = "device-level Vulkan functions missing";
	}
	return all;
}

bool Capturer::create_shm(size_t slot_bytes)
{
	shm_size_ = kHeaderBytes + slot_bytes * kSlots;
	shm_fd_ = shm_open(shm_name_.c_str(), O_CREAT | O_RDWR | O_TRUNC, 0600);
	if (shm_fd_ < 0) {
		error_ = "shm_open " + shm_name_ + " failed";
		return false;
	}
	if (ftruncate(shm_fd_, (off_t)shm_size_) != 0) {
		error_ = "ftruncate shm failed";
		return false;
	}
	void* p = mmap(nullptr, shm_size_, PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd_, 0);
	if (p == MAP_FAILED) {
		error_ = "mmap shm failed";
		return false;
	}
	shm_ = (uint8_t*)p;
	std::memset(shm_, 0, kHeaderBytes);
	std::memcpy(shm_, "VISORXR1", 8);
	put<uint32_t>(shm_, 8, 1);
	put<uint32_t>(shm_, 12, kSlots);
	put<uint64_t>(shm_, 16, slot_bytes);
	return true;
}

uint32_t Capturer::memory_type(uint32_t bits, VkMemoryPropertyFlags props)
{
	for (uint32_t i = 0; i < mem_props_.memoryTypeCount; i++) {
		if ((bits & (1u << i)) && (mem_props_.memoryTypes[i].propertyFlags & props) == props) {
			return i;
		}
	}
	return UINT32_MAX;
}

void Capturer::destroy_slot(Slot& s)
{
	VkDevice dev = binding_.device;
	if (s.mapped) {
		UnmapMemory(dev, s.buffer_mem);
	}
	if (s.buffer) {
		DestroyBuffer(dev, s.buffer, nullptr);
	}
	if (s.buffer_mem) {
		FreeMemory(dev, s.buffer_mem, nullptr);
	}
	if (s.image) {
		DestroyImage(dev, s.image, nullptr);
	}
	if (s.image_mem) {
		FreeMemory(dev, s.image_mem, nullptr);
	}
	// Fence and command buffers are kept: they don't depend on the size.
	s.image = VK_NULL_HANDLE;
	s.image_mem = VK_NULL_HANDLE;
	s.buffer = VK_NULL_HANDLE;
	s.buffer_mem = VK_NULL_HANDLE;
	s.mapped = nullptr;
	s.width = s.height = s.eye_w = 0;
}

bool Capturer::ensure_slot(Slot& s, uint32_t w, uint32_t h, uint32_t eye_w, VkFormat fmt)
{
	VkDevice dev = binding_.device;
	if (s.fence == VK_NULL_HANDLE) {
		VkFenceCreateInfo fci{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
		if (CreateFence(dev, &fci, nullptr, &s.fence) != VK_SUCCESS) {
			return false;
		}
		s.cmds.resize(5);
		VkCommandBufferAllocateInfo ai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
		ai.commandPool = pool_;
		ai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
		ai.commandBufferCount = (uint32_t)s.cmds.size();
		if (AllocateCommandBuffers(dev, &ai, s.cmds.data()) != VK_SUCCESS) {
			return false;
		}
	}
	if (s.image && s.width == w && s.height == h && s.format == fmt) {
		s.eye_w = eye_w;
		return true;
	}
	destroy_slot(s);

	VkImageCreateInfo ici{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
	ici.imageType = VK_IMAGE_TYPE_2D;
	ici.format = fmt;
	ici.extent = {w, h, 1};
	ici.mipLevels = 1;
	ici.arrayLayers = 1;
	ici.samples = VK_SAMPLE_COUNT_1_BIT;
	ici.tiling = VK_IMAGE_TILING_OPTIMAL;
	ici.usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
	ici.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
	if (CreateImage(dev, &ici, nullptr, &s.image) != VK_SUCCESS) {
		return false;
	}
	VkMemoryRequirements req;
	GetImageMemoryRequirements(dev, s.image, &req);
	VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
	mai.allocationSize = req.size;
	mai.memoryTypeIndex = memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
	if (mai.memoryTypeIndex == UINT32_MAX || AllocateMemory(dev, &mai, nullptr, &s.image_mem) != VK_SUCCESS ||
	    BindImageMemory(dev, s.image, s.image_mem, 0) != VK_SUCCESS) {
		destroy_slot(s);
		return false;
	}

	VkBufferCreateInfo bci{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
	bci.size = (VkDeviceSize)w * h * 4;
	bci.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
	bci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
	if (CreateBuffer(dev, &bci, nullptr, &s.buffer) != VK_SUCCESS) {
		destroy_slot(s);
		return false;
	}
	GetBufferMemoryRequirements(dev, s.buffer, &req);
	mai.allocationSize = req.size;
	// Cached host memory makes the CPU read-back several times faster.
	mai.memoryTypeIndex = memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
	                                                          VK_MEMORY_PROPERTY_HOST_COHERENT_BIT |
	                                                          VK_MEMORY_PROPERTY_HOST_CACHED_BIT);
	if (mai.memoryTypeIndex == UINT32_MAX) {
		mai.memoryTypeIndex = memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
		                                                          VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
	}
	if (mai.memoryTypeIndex == UINT32_MAX || AllocateMemory(dev, &mai, nullptr, &s.buffer_mem) != VK_SUCCESS ||
	    BindBufferMemory(dev, s.buffer, s.buffer_mem, 0) != VK_SUCCESS ||
	    MapMemory(dev, s.buffer_mem, 0, VK_WHOLE_SIZE, 0, &s.mapped) != VK_SUCCESS) {
		destroy_slot(s);
		return false;
	}
	s.width = w;
	s.height = h;
	s.eye_w = eye_w;
	s.format = fmt;
	return true;
}

VkCommandBuffer Capturer::next_blit_cmd(Slot& s)
{
	if (s.next_cmd >= s.cmds.size()) {
		VkCommandBufferAllocateInfo ai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
		ai.commandPool = pool_;
		ai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
		ai.commandBufferCount = 1;
		VkCommandBuffer cb = VK_NULL_HANDLE;
		if (AllocateCommandBuffers(binding_.device, &ai, &cb) != VK_SUCCESS) {
			return VK_NULL_HANDLE;
		}
		s.cmds.push_back(cb);
	}
	return s.cmds[s.next_cmd++];
}

void Capturer::configure(uint32_t eye_width, bool both_eyes, double fps)
{
	if (eye_width < 64) {
		eye_width = 64;
	}
	if (eye_width > kMaxEyeWidth) {
		eye_width = kMaxEyeWidth;
	}
	eye_width_ = eye_width;
	both_eyes_ = both_eyes;
	fps_ = fps < 0 ? 0 : fps;
}

uint64_t Capturer::request()
{
	std::lock_guard<std::mutex> g(mu_);
	requests_++;
	// A capture already in the pipe was rendered before this request.
	return seq_ + fenced_.size() + (active_ >= 0 ? 1 : 0);
}

void Capturer::on_release(VkImage image, VkFormat format, uint32_t img_w, uint32_t img_h,
                          const std::vector<CaptureView>& views, int64_t frame)
{
	if (!ok_ || views.empty()) {
		return;
	}
	std::lock_guard<std::mutex> g(mu_);

	if (decided_frame_ != frame) {
		decided_frame_ = frame;
		if (active_ >= 0) {
			// Last frame recorded blits but never reached xrEndFrame: fence it
			// off so its command buffers are known idle before reuse.
			Slot& old = slots_[active_];
			ResetFences(binding_.device, 1, &old.fence);
			if (QueueSubmit(queue_, 0, nullptr, old.fence) == VK_SUCCESS) {
				old.in_flight = true;
				old.frame = -1; // discard on completion
				fenced_.push_back(active_);
				cv_.notify_all();
			}
			active_ = -1;
		}
		int64_t now = now_ns();
		double fps = fps_;
		bool due = every_frame_ || requests_ > 0 ||
		           (fps > 0 && (double)(now - last_capture_ns_) >= 1e9 / fps - 2e6);
		if (due) {
			for (int i = 0; i < kSlots; i++) {
				if (!slots_[i].in_flight) {
					active_ = i;
					active_frame_ = frame;
					slots_[i].initialized = false;
					slots_[i].next_cmd = 1;
					slots_[i].eyes = 0;
					break;
				}
			}
		}
	}
	if (active_ < 0 || active_frame_ != frame) {
		return;
	}

	VkFormatProperties fp{};
	GetPhysicalDeviceFormatProperties(binding_.physicalDevice, format, &fp);
	if (!(fp.optimalTilingFeatures & VK_FORMAT_FEATURE_BLIT_SRC_BIT)) {
		if (!warned_format_) {
			LOGW("capture: swapchain format %d can't be blitted, frames won't be captured", (int)format);
			warned_format_ = true;
		}
		return;
	}

	bool both = both_eyes_;
	std::vector<CaptureView> wanted;
	for (const auto& v : views) {
		if (v.eye == 0 || (both && v.eye == 1)) {
			wanted.push_back(v);
		}
	}
	if (wanted.empty()) {
		return;
	}
	const XrRect2Di& r0 = wanted[0].rect;
	if (r0.extent.width <= 0 || r0.extent.height <= 0) {
		return;
	}
	uint32_t eye_w = eye_width_;
	if (eye_w > (uint32_t)r0.extent.width) {
		eye_w = (uint32_t)r0.extent.width;
	}
	uint32_t eye_h = (uint32_t)((double)eye_w * r0.extent.height / r0.extent.width + 0.5);
	if (eye_h > kMaxHeight) {
		eye_h = kMaxHeight;
	}
	uint32_t total_w = both ? eye_w * 2 : eye_w;
	VkFormat fmt = srgb_like(format) ? VK_FORMAT_R8G8B8A8_SRGB : VK_FORMAT_R8G8B8A8_UNORM;

	Slot& s = slots_[active_];
	if (!s.initialized && !ensure_slot(s, total_w, eye_h, eye_w, fmt)) {
		LOGW("capture: could not allocate %ux%u staging image", total_w, eye_h);
		active_ = -1;
		return;
	}
	if (s.initialized && (s.eye_w != eye_w || s.format != fmt)) {
		return; // a second swapchain disagrees on size; keep the first
	}

	VkCommandBuffer cb = next_blit_cmd(s);
	if (cb == VK_NULL_HANDLE) {
		return;
	}
	ResetCommandBuffer(cb, 0);
	VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
	bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
	BeginCommandBuffer(cb, &bi);

	VkImageMemoryBarrier pre[2];
	uint32_t npre = 0;
	pre[npre++] = image_barrier(image, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
	                            VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
	if (!s.initialized) {
		pre[npre++] = image_barrier(s.image, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0,
		                            VK_ACCESS_TRANSFER_WRITE_BIT);
		s.initialized = true;
	}
	CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT,
	                   VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, npre, pre);

	for (const auto& v : wanted) {
		VkImageBlit blit{};
		blit.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, v.array_index, 1};
		blit.srcOffsets[0] = {v.rect.offset.x, v.rect.offset.y, 0};
		blit.srcOffsets[1] = {v.rect.offset.x + v.rect.extent.width, v.rect.offset.y + v.rect.extent.height, 1};
		int32_t dx = (both && v.eye == 1) ? (int32_t)eye_w : 0;
		blit.dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
		blit.dstOffsets[0] = {dx, 0, 0};
		blit.dstOffsets[1] = {dx + (int32_t)eye_w, (int32_t)eye_h, 1};
		// Clamp to the image in case the rect lags a resize.
		if (blit.srcOffsets[1].x > (int32_t)img_w || blit.srcOffsets[1].y > (int32_t)img_h) {
			continue;
		}
		CmdBlitImage(cb, image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, s.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
		             1, &blit, VK_FILTER_LINEAR);
		s.eyes |= 1u << v.eye;
	}

	VkImageMemoryBarrier post = image_barrier(image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
	                                          VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL, 0,
	                                          VK_ACCESS_COLOR_ATTACHMENT_READ_BIT |
	                                              VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT);
	CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT,
	                   VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, 0, 0,
	                   nullptr, 0, nullptr, 1, &post);
	EndCommandBuffer(cb);

	VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
	si.commandBufferCount = 1;
	si.pCommandBuffers = &cb;
	if (QueueSubmit(queue_, 1, &si, VK_NULL_HANDLE) != VK_SUCCESS) {
		LOGW("capture: blit submit failed");
	}
}

void Capturer::on_end_frame(int64_t frame, int64_t display_time)
{
	if (!ok_) {
		return;
	}
	std::lock_guard<std::mutex> g(mu_);
	if (active_ < 0 || active_frame_ != frame) {
		return;
	}
	Slot& s = slots_[active_];
	int idx = active_;
	active_ = -1;
	if (!s.initialized || s.eyes == 0) {
		return;
	}

	VkCommandBuffer cb = s.cmds[0];
	ResetCommandBuffer(cb, 0);
	VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
	bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
	BeginCommandBuffer(cb, &bi);
	VkImageMemoryBarrier to_src = image_barrier(s.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
	                                            VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_ACCESS_TRANSFER_WRITE_BIT,
	                                            VK_ACCESS_TRANSFER_READ_BIT);
	CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr,
	                   1, &to_src);
	VkBufferImageCopy copy{};
	copy.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
	copy.imageExtent = {s.width, s.height, 1};
	CmdCopyImageToBuffer(cb, s.image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, s.buffer, 1, &copy);
	VkBufferMemoryBarrier host{VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER};
	host.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
	host.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
	host.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
	host.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
	host.buffer = s.buffer;
	host.size = VK_WHOLE_SIZE;
	CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 0, nullptr, 1, &host, 0,
	                   nullptr);
	EndCommandBuffer(cb);

	ResetFences(binding_.device, 1, &s.fence);
	VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
	si.commandBufferCount = 1;
	si.pCommandBuffers = &cb;
	if (QueueSubmit(queue_, 1, &si, s.fence) != VK_SUCCESS) {
		LOGW("capture: copy submit failed");
		return;
	}
	s.in_flight = true;
	s.frame = frame;
	s.display_time = display_time;
	fenced_.push_back(idx);
	last_capture_ns_ = now_ns();
	requests_ = 0;
	cv_.notify_all();
}

void Capturer::worker()
{
	while (true) {
		int idx;
		{
			std::unique_lock<std::mutex> lk(mu_);
			cv_.wait(lk, [this] { return stop_ || !fenced_.empty(); });
			if (stop_) {
				return;
			}
			idx = fenced_.front();
		}
		Slot& s = slots_[idx];
		VkResult r;
		do {
			r = WaitForFences(binding_.device, 1, &s.fence, VK_TRUE, 100 * 1000 * 1000ull);
			if (stop_) {
				return;
			}
		} while (r == VK_TIMEOUT);

		CaptureInfo info;
		bool publish = r == VK_SUCCESS && s.frame >= 0 && s.mapped;
		if (publish) {
			size_t bytes = (size_t)s.width * s.height * 4;
			uint32_t k = shm_slot_++ % kSlots;
			size_t hdr = 64 + 64 * (size_t)k;
			store_release<uint64_t>(shm_, hdr, 0); // writing
			std::memcpy(shm_ + kHeaderBytes + slot_bytes_ * k, s.mapped, bytes);
			info.width = s.width;
			info.height = s.height;
			info.eyes = s.eyes;
			info.frame = s.frame;
			info.display_time = s.display_time;
			info.capture_ns = now_ns();
			put<uint32_t>(shm_, hdr + 8, s.width);
			put<uint32_t>(shm_, hdr + 12, s.height);
			put<uint32_t>(shm_, hdr + 16, s.width * 4);
			put<uint32_t>(shm_, hdr + 20, s.eyes);
			put<int64_t>(shm_, hdr + 24, s.frame);
			put<int64_t>(shm_, hdr + 32, s.display_time);
			put<int64_t>(shm_, hdr + 40, info.capture_ns);
			put<uint32_t>(shm_, hdr + 48, 0);
			std::lock_guard<std::mutex> g(mu_);
			info.seq = ++seq_;
			store_release<uint64_t>(shm_, hdr, info.seq);
			put<uint32_t>(shm_, 32, k);
			store_release<uint64_t>(shm_, 24, info.seq);
		}
		{
			std::lock_guard<std::mutex> g(mu_);
			s.in_flight = false;
			if (!fenced_.empty() && fenced_.front() == idx) {
				fenced_.pop_front();
			}
			if (publish) {
				latest_ = info;
			}
		}
		pub_cv_.notify_all();
	}
}

bool Capturer::wait(uint64_t after, int timeout_ms, CaptureInfo* out)
{
	std::unique_lock<std::mutex> lk(mu_);
	bool got = pub_cv_.wait_for(lk, std::chrono::milliseconds(timeout_ms),
	                            [&] { return stop_ || latest_.seq > after; });
	if (out) {
		*out = latest_;
	}
	return got && latest_.seq > after;
}

CaptureInfo Capturer::latest() const
{
	std::lock_guard<std::mutex> g(mu_);
	return latest_;
}

void Capturer::shutdown()
{
	{
		std::lock_guard<std::mutex> g(mu_);
		if (stop_) {
			return;
		}
		stop_ = true;
	}
	cv_.notify_all();
	pub_cv_.notify_all();
	if (thread_.joinable()) {
		thread_.join();
	}
	if (ok_) {
		VkDevice dev = binding_.device;
		for (auto& s : slots_) {
			if (s.in_flight && s.fence) {
				WaitForFences(dev, 1, &s.fence, VK_TRUE, 1000ull * 1000 * 1000);
			}
			destroy_slot(s);
			if (s.fence) {
				DestroyFence(dev, s.fence, nullptr);
				s.fence = VK_NULL_HANDLE;
			}
		}
		if (pool_) {
			DestroyCommandPool(dev, pool_, nullptr);
			pool_ = VK_NULL_HANDLE;
		}
	}
	if (shm_) {
		munmap(shm_, shm_size_);
		shm_ = nullptr;
	}
	if (shm_fd_ >= 0) {
		close(shm_fd_);
		shm_fd_ = -1;
		shm_unlink(shm_name_.c_str());
	}
	ok_ = false;
}

// ---------------------------------------------------------------------------
// RuntimeFrames

namespace {
template <typename T> T get(const uint8_t* base, size_t off)
{
	T v;
	std::memcpy(&v, base + off, sizeof(T));
	return v;
}
uint64_t load_acquire(const uint8_t* base, size_t off)
{
	return __atomic_load_n(reinterpret_cast<const uint64_t*>(base + off), __ATOMIC_ACQUIRE);
}
} // namespace

RuntimeFrames::RuntimeFrames(std::string shm_name) : name_(std::move(shm_name))
{
	int fd = shm_open(name_.c_str(), O_RDWR, 0);
	if (fd < 0) {
		error_ = "runtime publishes no frames (shm " + name_ + " missing)";
		return;
	}
	struct stat st{};
	if (fstat(fd, &st) != 0 || st.st_size < (off_t)kHeaderBytes) {
		close(fd);
		error_ = "runtime frame buffer too small";
		return;
	}
	void* p = mmap(nullptr, (size_t)st.st_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	close(fd);
	if (p == MAP_FAILED) {
		error_ = "can't map runtime frame buffer";
		return;
	}
	uint8_t* b = static_cast<uint8_t*>(p);
	if (std::memcmp(b, "VISORXR1", 8) != 0 || get<uint32_t>(b, 36) != 2) {
		munmap(p, (size_t)st.st_size);
		error_ = "shm " + name_ + " is not a runtime frame buffer";
		return;
	}
	shm_ = b;
	size_ = (size_t)st.st_size;
	fps_ = get<double>(shm_, 48);
}

RuntimeFrames::~RuntimeFrames() { shutdown(); }

void RuntimeFrames::shutdown()
{
	if (shm_) {
		munmap(shm_, size_);
		shm_ = nullptr; // the runtime owns (and unlinks) it
	}
}

void RuntimeFrames::write_fps(double fps) { std::memcpy(shm_ + 48, &fps, sizeof(fps)); }

void RuntimeFrames::configure(uint32_t eye_width, bool both_eyes, double fps)
{
	if (!shm_) {
		return;
	}
	put<uint32_t>(shm_, 56, eye_width < 64 ? 64 : (eye_width > kMaxEyeWidth ? kMaxEyeWidth : eye_width));
	put<uint32_t>(shm_, 60, both_eyes ? 3u : 1u);
	fps_ = fps < 0 ? 0 : fps;
	write_fps(every_frame_ ? 1000.0 : fps_);
}

uint32_t RuntimeFrames::eye_width() const { return shm_ ? get<uint32_t>(shm_, 56) : 0; }

bool RuntimeFrames::both_eyes() const { return shm_ && get<uint32_t>(shm_, 60) == 3; }

void RuntimeFrames::set_every_frame(bool on)
{
	every_frame_ = on;
	if (shm_) {
		write_fps(on ? 1000.0 : fps_);
	}
}

uint64_t RuntimeFrames::request()
{
	if (!shm_) {
		return 0;
	}
	// Our ticket is the request number; a capture that served it was taken
	// after this moment.
	return __atomic_add_fetch(reinterpret_cast<uint64_t*>(shm_ + 40), 1, __ATOMIC_ACQ_REL);
}

bool RuntimeFrames::wait(uint64_t ticket, int timeout_ms, CaptureInfo* out)
{
	if (!shm_) {
		return false;
	}
	auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
	while (load_acquire(shm_, 256) < ticket || load_acquire(shm_, 24) == 0) {
		if (std::chrono::steady_clock::now() >= deadline) {
			if (out) {
				*out = latest();
			}
			return false;
		}
		std::this_thread::sleep_for(std::chrono::milliseconds(2));
	}
	if (out) {
		*out = latest();
	}
	return true;
}

CaptureInfo RuntimeFrames::latest() const
{
	CaptureInfo c;
	if (!shm_) {
		return c;
	}
	uint32_t slot = get<uint32_t>(shm_, 32);
	size_t hdr = 64 + 64 * (size_t)slot;
	c.seq = load_acquire(shm_, hdr);
	c.width = get<uint32_t>(shm_, hdr + 8);
	c.height = get<uint32_t>(shm_, hdr + 12);
	c.eyes = get<uint32_t>(shm_, hdr + 20);
	c.frame = -1; // the layer maps display_time to its own frame index
	c.display_time = get<int64_t>(shm_, hdr + 32);
	c.capture_ns = get<int64_t>(shm_, hdr + 40);
	return c;
}

} // namespace visor
