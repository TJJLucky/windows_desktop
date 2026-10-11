// Event-driven, persistent WGC. WinRT and the D3D context belong to one MTA thread.
#include "pch.h"
#include <d3d11.h>
#include <dxgi1_2.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <thread>
#include <memory>
#include <limits>
#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "windowsapp.lib")
using namespace winrt;
namespace WGC = winrt::Windows::Graphics::Capture;
namespace WDX = winrt::Windows::Graphics::DirectX;
namespace WDX3D = winrt::Windows::Graphics::DirectX::Direct3D11;
struct WgcFrameInfo {
    uint32_t structSize, width, height, requiredBytes;
    uint64_t generation, frameNumber;
    int64_t frameTime100ns, arrivalTime100ns;
    int32_t error, hresult;
};
enum Status : int { OK = 1, INVALID_ARGUMENT = -1, WINDOW_UNAVAILABLE = -2,
    TIMEOUT = -3, BUFFER_TOO_SMALL = -4, SESSION_CHANGED = -5,
    DEVICE_LOST = -6, CAPTURE_FAILED = -7, CLOSED = -8 };
static int64_t Clock100ns() {
    LARGE_INTEGER qpc, frequency;
    QueryPerformanceCounter(&qpc); QueryPerformanceFrequency(&frequency);
    return (qpc.QuadPart / frequency.QuadPart) * 10000000LL +
        (qpc.QuadPart % frequency.QuadPart) * 10000000LL / frequency.QuadPart;
}
static bool Available(HWND hwnd, DWORD pid) {
    DWORD actual = 0; GetWindowThreadProcessId(hwnd, &actual);
    return IsWindow(hwnd) && IsWindowVisible(hwnd) && !IsIconic(hwnd) && actual == pid;
}
static std::atomic<uint64_t> nextGeneration{ 0 };
class Capture;
static thread_local Capture* eventCapture = nullptr;
struct Signal {
    HANDLE wake = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    std::atomic<bool> windowEnded{ false };
    std::atomic<int64_t> arrival{ 0 };
    ~Signal() { if (wake) CloseHandle(wake); }
};
class Capture {
public:
    HWND hwnd; DWORD pid = 0;
    const uint64_t id = ++nextGeneration;
    std::shared_ptr<Signal> signal = std::make_shared<Signal>();
    HANDLE wake = signal->wake;
    std::atomic<bool> stopping{ false };
    std::mutex mutex;
    std::condition_variable done;
    bool initialized = false;
    int state = CAPTURE_FAILED;
    int32_t lastHr = 0;
    struct Read {
        unsigned char* buffer; uint32_t capacity; int64_t barrier;
        ULONGLONG deadline; WgcFrameInfo* info; bool completed = false; int status = TIMEOUT;
    };
    Read* pending = nullptr;
    std::thread worker;
    explicit Capture(HWND target) : hwnd(target) {
        GetWindowThreadProcessId(hwnd, &pid);
        if (!wake) throw hresult_error(HRESULT_FROM_WIN32(GetLastError()));
        worker = std::thread([this] { Run(); });
    }
    ~Capture() {
        stopping = true; SetEvent(wake);
        if (worker.joinable()) worker.join();
    }
    int Open() {
        std::unique_lock lock(mutex);
        done.wait(lock, [this] { return initialized; });
        return state;
    }
    int ReadFrame(unsigned char* buffer, uint32_t capacity, int64_t barrier,
                  uint32_t timeout, WgcFrameInfo* info) {
        std::unique_lock lock(mutex);
        if (state != OK) { info->error = state; info->hresult = lastHr; return state; }
        Read request{ buffer, capacity, barrier, GetTickCount64() + timeout, info };
        pending = &request; SetEvent(wake);
        done.wait(lock, [&request] { return request.completed; });
        return request.status;
    }
    static void CALLBACK WindowEvent(HWINEVENTHOOK, DWORD event, HWND target,
        LONG object, LONG, DWORD, DWORD) {
        auto self = eventCapture;
        if (!self || target != self->hwnd) return;
        if (event == EVENT_SYSTEM_MINIMIZESTART ||
            ((event == EVENT_OBJECT_DESTROY || event == EVENT_OBJECT_HIDE) && object == OBJID_WINDOW))
            self->signal->windowEnded = true;
        SetEvent(self->wake);
    }
private:
    void Complete(int status) {
        pending->status = status; pending->info->error = status;
        pending->info->hresult = lastHr;
        pending->completed = true; pending = nullptr; done.notify_all();
    }
    void Run() noexcept {
        WGC::Direct3D11CaptureFramePool pool{ nullptr };
        WGC::GraphicsCaptureSession session{ nullptr };
        WGC::GraphicsCaptureItem item{ nullptr };
        event_token arrived{}, closed{};
        bool subscribed = false, apartment = false;
        HWINEVENTHOOK objectHook = nullptr, minimizeHook = nullptr;
        int result = CLOSED;
        try {
            init_apartment(apartment_type::multi_threaded); apartment = true;
            if (!Available(hwnd, pid)) throw hresult_error(HRESULT_FROM_WIN32(ERROR_INVALID_WINDOW_HANDLE));
            auto interop = get_activation_factory<WGC::GraphicsCaptureItem, IGraphicsCaptureItemInterop>();
            check_hresult(interop->CreateForWindow(hwnd, guid_of<WGC::GraphicsCaptureItem>(), put_abi(item)));
            // Windows 10 19045 can dispatch a runtime callback after Close + RoUninitialize.
            // Keep the runtime code mapped until process exit; capture/device resources still
            // close on this thread. Never unload its code while its callbacks can be running.
            HMODULE runtime = nullptr;
            if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_PIN, L"GraphicsCapture.dll", &runtime))
                throw hresult_error(HRESULT_FROM_WIN32(GetLastError()));
            auto size = item.Size();
            com_ptr<ID3D11Device> device;
            com_ptr<ID3D11DeviceContext> context;
            check_hresult(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
                D3D11_CREATE_DEVICE_BGRA_SUPPORT, nullptr, 0, D3D11_SDK_VERSION,
                device.put(), nullptr, context.put()));
            auto dxgi = device.as<IDXGIDevice>();
            com_ptr<IInspectable> inspectable;
            check_hresult(CreateDirect3D11DeviceFromDXGIDevice(dxgi.get(), inspectable.put()));
            auto rtDevice = inspectable.as<WDX3D::IDirect3DDevice>();
            pool = WGC::Direct3D11CaptureFramePool::CreateFreeThreaded(rtDevice,
                WDX::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2, size);
            // Event callbacks only wake the owner; no D3D calls or pixel reads here.
            arrived = pool.FrameArrived([s = signal](auto const&, auto const&) {
                s->arrival = Clock100ns(); SetEvent(s->wake);
            });
            closed = item.Closed([s = signal](auto const&, auto const&) {
                s->windowEnded = true; SetEvent(s->wake);
            });
            subscribed = true;
            eventCapture = this;
            objectHook = SetWinEventHook(EVENT_OBJECT_DESTROY, EVENT_OBJECT_HIDE, nullptr,
                WindowEvent, pid, 0, WINEVENT_OUTOFCONTEXT);
            minimizeHook = SetWinEventHook(EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND,
                nullptr, WindowEvent, pid, 0, WINEVENT_OUTOFCONTEXT);
            if (!objectHook || !minimizeHook) throw hresult_error(HRESULT_FROM_WIN32(GetLastError()));
            session = pool.CreateCaptureSession(item);
            try { session.IsBorderRequired(false); } catch (hresult_error const&) {}
            session.StartCapture();
            com_ptr<ID3D11Texture2D> latest, staging;
            uint64_t generation = id, number = 0;
            bool established = false;
            int64_t frameTime = 0, arrivalTime = 0;
            auto makeTextures = [&] {
                D3D11_TEXTURE2D_DESC desc{};
                desc.Width = size.Width; desc.Height = size.Height;
                desc.MipLevels = desc.ArraySize = desc.SampleDesc.Count = 1;
                desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
                desc.Usage = D3D11_USAGE_DEFAULT;
                latest = nullptr; staging = nullptr;
                check_hresult(device->CreateTexture2D(&desc, nullptr, latest.put()));
                desc.Usage = D3D11_USAGE_STAGING; desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
                check_hresult(device->CreateTexture2D(&desc, nullptr, staging.put()));
            };
            if (size.Width <= 0 || size.Height <= 0) throw hresult_error(E_INVALIDARG);
            makeTextures();
            { std::lock_guard lock(mutex); state = OK; initialized = true; done.notify_all(); }
            while (!stopping) {
                MSG message;
                while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
                    TranslateMessage(&message); DispatchMessageW(&message);
                }
                if (signal->windowEnded || !Available(hwnd, pid)) { result = WINDOW_UNAVAILABLE; break; }
                // Drain WGC buffers promptly; retain only our latest GPU image.
                auto observedArrival = signal->arrival.load();
                while (auto frame = pool.TryGetNextFrame()) {
                    auto content = frame.ContentSize();
                    if (content.Width <= 0 || content.Height <= 0) { frame.Close(); continue; }
                    auto relativeTime = frame.SystemRelativeTime().count();
                    auto access = frame.Surface().as<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
                    com_ptr<ID3D11Texture2D> source;
                    check_hresult(access->GetInterface(__uuidof(ID3D11Texture2D), source.put_void()));
                    D3D11_TEXTURE2D_DESC desc{}; source->GetDesc(&desc);
                    // Release the WGC frame as soon as the surface is retained; the source
                    // texture stays alive through the com_ptr below.
                    frame.Close();
                    if (content.Width != size.Width || content.Height != size.Height) {
                        // The first frame can disagree with GraphicsCaptureItem.Size(); adopt it
                        // silently as the session baseline. Later changes are a real resize and
                        // bump the generation so callers can observe that identity moved.
                        size = content;
                        if (established) { generation = ++nextGeneration; number = 0; frameTime = arrivalTime = 0; }
                        makeTextures();
                        pool.Recreate(rtDevice, WDX::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2, size);
                    }
                    if (desc.Width < static_cast<UINT>(size.Width) || desc.Height < static_cast<UINT>(size.Height)) {
                        continue;
                    }
                    D3D11_BOX box{ 0, 0, 0, static_cast<UINT>(size.Width), static_cast<UINT>(size.Height), 1 };
                    context->CopySubresourceRegion(latest.get(), 0, 0, 0, 0, source.get(), 0, &box);
                    frameTime = relativeTime; arrivalTime = observedArrival;
                    ++number; established = true;
                }
                check_hresult(device->GetDeviceRemovedReason());
                DWORD wait = INFINITE;
                {
                    std::lock_guard lock(mutex);
                    if (pending) {
                        auto info = pending->info;
                        info->width = size.Width; info->height = size.Height;
                        info->generation = generation; info->frameNumber = number;
                        info->frameTime100ns = frameTime;
                        info->arrivalTime100ns = arrivalTime;
                        uint64_t bytes = uint64_t(size.Width) * size.Height * 4;
                        if (bytes > (std::numeric_limits<uint32_t>::max)()) Complete(CAPTURE_FAILED);
                        else {
                            info->requiredBytes = static_cast<uint32_t>(bytes);
                            if (number && frameTime > pending->barrier && arrivalTime > pending->barrier) {
                                if (!pending->buffer || pending->capacity < bytes) Complete(BUFFER_TOO_SMALL);
                                else {
                                    context->CopyResource(staging.get(), latest.get());
                                    D3D11_MAPPED_SUBRESOURCE mapped{};
                                    check_hresult(context->Map(staging.get(), 0, D3D11_MAP_READ, 0, &mapped));
                                    for (int y = 0; y < size.Height; ++y)
                                        memcpy(pending->buffer + uint64_t(y) * size.Width * 4,
                                            static_cast<unsigned char*>(mapped.pData) + uint64_t(y) * mapped.RowPitch, size.Width * 4);
                                    context->Unmap(staging.get(), 0);
                                    Complete(OK);
                                }
                            }
                            else if (GetTickCount64() >= pending->deadline) Complete(TIMEOUT);
                        }
                        if (pending) {
                            auto now = GetTickCount64();
                            wait = now >= pending->deadline ? 0 : static_cast<DWORD>(pending->deadline - now);
                        }
                    }
                }
                MsgWaitForMultipleObjects(1, &wake, FALSE, wait, QS_ALLINPUT);
            }
        } catch (hresult_error const& error) {
            lastHr = error.code().value;
            result = (lastHr == DXGI_ERROR_DEVICE_REMOVED || lastHr == DXGI_ERROR_DEVICE_RESET ||
                lastHr == DXGI_ERROR_DEVICE_HUNG) ? DEVICE_LOST : CAPTURE_FAILED;
            if (!Available(hwnd, pid)) result = WINDOW_UNAVAILABLE;
        } catch (...) { result = CAPTURE_FAILED; }
        // Explicit shutdown only; no blocking cleanup in DllMain.
        if (objectHook) UnhookWinEvent(objectHook);
        if (minimizeHook) UnhookWinEvent(minimizeHook);
        eventCapture = nullptr;
        try {
            if (subscribed) { pool.FrameArrived(arrived); item.Closed(closed); }
            if (session) session.Close();
            if (pool) pool.Close();
        } catch (...) {}
        session = nullptr; pool = nullptr; item = nullptr;
        winrt::clear_factory_cache();
        if (apartment) uninit_apartment();
        std::lock_guard lock(mutex);
        state = result; initialized = true;
        if (pending) Complete(result);
        done.notify_all();
    }
};
// Avoid static resource destruction under the Windows loader lock. Call WgcClose before unload.
struct Engine { std::mutex api; std::unique_ptr<Capture> capture; };
static Engine& GetEngine() { static Engine* engine = new Engine; return *engine; }
extern "C" __declspec(dllexport) int64_t WgcClock100ns() { return Clock100ns(); }
extern "C" __declspec(dllexport) int WgcOpen(HWND hwnd, uint64_t* outSession) {
    if (!outSession || !IsWindow(hwnd) || !IsWindowVisible(hwnd) || IsIconic(hwnd)) return WINDOW_UNAVAILABLE;
    auto& engine = GetEngine(); std::lock_guard lock(engine.api);
    try {
        if (engine.capture && engine.capture->hwnd == hwnd) {
            int status = engine.capture->Open();
            if (status == OK) { *outSession = engine.capture->id; return OK; }
            // A restored window may reopen. A device/capture fault must reach the caller first.
            if (status != WINDOW_UNAVAILABLE && status != CLOSED) return status;
        }
        engine.capture.reset(); engine.capture = std::make_unique<Capture>(hwnd);
        int status = engine.capture->Open();
        if (status != OK) { engine.capture.reset(); return status; }
        *outSession = engine.capture->id; return OK;
    } catch (...) { engine.capture.reset(); return CAPTURE_FAILED; }
}
extern "C" __declspec(dllexport) int WgcRead(uint64_t session, unsigned char* buffer,
    uint32_t capacity, int64_t barrier100ns, uint32_t timeoutMs, WgcFrameInfo* info) {
    if (!info || info->structSize != sizeof(WgcFrameInfo) || barrier100ns < 0 || timeoutMs > 2000) return INVALID_ARGUMENT;
    *info = {}; info->structSize = sizeof(WgcFrameInfo);
    auto& engine = GetEngine(); std::lock_guard lock(engine.api);
    if (!engine.capture || engine.capture->id != session) { info->error = SESSION_CHANGED; return SESSION_CHANGED; }
    return engine.capture->ReadFrame(buffer, capacity, barrier100ns, timeoutMs, info);
}
extern "C" __declspec(dllexport) int WgcClose(uint64_t session) {
    auto& engine = GetEngine(); std::lock_guard lock(engine.api);
    if (engine.capture && (!session || engine.capture->id == session)) engine.capture.reset();
    return OK;
}
extern "C" __declspec(dllexport) int WgcSnapshot(HWND hwnd, unsigned char* buffer, int capacity, int* width, int* height) {
    if (!width || !height || capacity < 0) return 0;
    uint64_t session = 0;
    if (WgcOpen(hwnd, &session) != OK) return 0;
    WgcFrameInfo info{}; info.structSize = sizeof(info);
    int status = WgcRead(session, buffer, static_cast<uint32_t>(capacity), Clock100ns(), 300, &info);
    *width = info.width; *height = info.height;
    return status == OK || (!buffer && status == BUFFER_TOO_SMALL) ? 1 : 0;
}
