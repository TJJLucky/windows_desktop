// wgc.cpp — WGC 单次窗口快照 DLL
// 特性：无全局状态、每次调用自建自毁、硬件/WARP双轨、异常全兜底
#include "pch.h"
#include <d3d11.h>
#include <dxgi1_2.h>
#include <inspectable.h>
#include <cstring>
#include <cstdio>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "windowsapp.lib")

#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>

using namespace winrt;
namespace WGC = winrt::Windows::Graphics::Capture;
namespace WDX = winrt::Windows::Graphics::DirectX;
namespace WDX3D = winrt::Windows::Graphics::DirectX::Direct3D11;
namespace WGFX = winrt::Windows::Graphics;

// WGC COM互操作接口
struct __declspec(uuid("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356"))
IGraphicsCaptureItemInterop : IUnknown
{
    virtual HRESULT STDMETHODCALLTYPE CreateForWindow(HWND hwnd, REFIID riid, void** ppv) = 0;
    virtual HRESULT STDMETHODCALLTYPE CreateForMonitor(HMONITOR hmon, REFIID riid, void** ppv) = 0;
};

struct __declspec(uuid("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1"))
IDirect3DDxgiInterfaceAccess : IUnknown
{
    virtual HRESULT STDMETHODCALLTYPE GetInterface(REFIID riid, void** ppv) = 0;
};

// 导出：单次窗口快照（无状态、调用即清理）
// 返回：1=成功 / 0=失败
extern "C" __declspec(dllexport)
int WgcSnapshot(HWND hwnd, unsigned char* buf, int bufSize, int* outW, int* outH)
{
    if (!IsWindow(hwnd) || !outW || !outH)
        return 0;

    ID3D11Device* d3dDev = nullptr;
    ID3D11DeviceContext* devCtx = nullptr;
    ID3D11Texture2D* stagingTex = nullptr;
    WGC::Direct3D11CaptureFramePool framePool{ nullptr };
    WGC::GraphicsCaptureSession captureSession{ nullptr };

    auto cleanup = [&]()
    {
        if (stagingTex) { stagingTex->Release(); stagingTex = nullptr; }
        if (captureSession) { captureSession.Close(); captureSession = nullptr; }
        if (framePool) { framePool.Close(); framePool = nullptr; }
        if (devCtx) { devCtx->Release(); devCtx = nullptr; }
        if (d3dDev) { d3dDev->Release(); d3dDev = nullptr; }
    };

    try
    {
        // 1. 创建窗口捕获对象
        auto captureFactory = get_activation_factory<WGC::GraphicsCaptureItem>();
        auto interop = captureFactory.as<IGraphicsCaptureItemInterop>();
        WGC::GraphicsCaptureItem captureItem{ nullptr };
        check_hresult(interop->CreateForWindow(
            hwnd, guid_of<WGC::GraphicsCaptureItem>(), put_abi(captureItem)));

        auto winSize = captureItem.Size();
        int width = static_cast<int>(winSize.Width);
        int height = static_cast<int>(winSize.Height);
        if (width <= 0 || height <= 0)
            return 0;

        *outW = width;
        *outH = height;
        int totalBytes = width * height * 4;

        // 仅查询尺寸：buf 传 NULL 直接返回成功
        if (buf == nullptr)
            return 1;
        if (bufSize < totalBytes)
            return 0;

        // 2. 创建 D3D 设备（硬件优先，失败切 WARP）
        HRESULT hr = D3D11CreateDevice(
            nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
            D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            nullptr, 0, D3D11_SDK_VERSION,
            &d3dDev, nullptr, &devCtx);
        if (FAILED(hr))
        {
            hr = D3D11CreateDevice(
                nullptr, D3D_DRIVER_TYPE_WARP, nullptr,
                D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                nullptr, 0, D3D11_SDK_VERSION,
                &d3dDev, nullptr, &devCtx);
            check_hresult(hr);
        }

        // 3. DXGI 设备转 WinRT D3D 设备
        com_ptr<IDXGIDevice> dxgiDev;
        check_hresult(d3dDev->QueryInterface(IID_PPV_ARGS(&dxgiDev)));

        using CreateDXGIDeviceFn = HRESULT(WINAPI*)(IDXGIDevice*, IInspectable**);
        HMODULE hD3d11 = LoadLibraryW(L"d3d11.dll");
        if (!hD3d11)
            throw hresult_error(E_FAIL);
        auto createFn = reinterpret_cast<CreateDXGIDeviceFn>(
            GetProcAddress(hD3d11, "CreateDirect3D11DeviceFromDXGIDevice"));
        if (!createFn)
        {
            FreeLibrary(hD3d11);
            throw hresult_error(E_FAIL);
        }

        IInspectable* rtDevIns = nullptr;
        check_hresult(createFn(dxgiDev.get(), &rtDevIns));
        WDX3D::IDirect3DDevice rtDevice{ nullptr };
        attach_abi(rtDevice, rtDevIns);
        FreeLibrary(hD3d11);

        // 4. 帧池 + 捕获会话
        WGFX::SizeInt32 sizeInt{ width, height };
        framePool = WGC::Direct3D11CaptureFramePool::CreateFreeThreaded(
            rtDevice, WDX::DirectXPixelFormat::B8G8R8A8UIntNormalized, 1, sizeInt);
        captureSession = framePool.CreateCaptureSession(captureItem);
        try { captureSession.IsBorderRequired(false); } catch (...) {}
        captureSession.StartCapture();

        // 5. 等待画面就绪（最多 300ms）
        WGC::Direct3D11CaptureFrame frame{ nullptr };
        for (int i = 0; i < 30; ++i)
        {
            frame = framePool.TryGetNextFrame();
            if (frame) break;
            Sleep(10);
        }
        if (!frame)
            throw hresult_error(E_FAIL);

        // 6. 取出 GPU 纹理
        com_ptr<IDirect3DDxgiInterfaceAccess> surfaceAccess =
            frame.Surface().as<IDirect3DDxgiInterfaceAccess>();
        com_ptr<ID3D11Texture2D> srcTex;
        check_hresult(surfaceAccess->GetInterface(IID_PPV_ARGS(&srcTex)));

        // 7. 创建暂存纹理（CPU 可读）
        D3D11_TEXTURE2D_DESC stagingDesc{};
        stagingDesc.Width = width;
        stagingDesc.Height = height;
        stagingDesc.MipLevels = 1;
        stagingDesc.ArraySize = 1;
        stagingDesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        stagingDesc.SampleDesc.Count = 1;
        stagingDesc.Usage = D3D11_USAGE_STAGING;
        stagingDesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;

        check_hresult(d3dDev->CreateTexture2D(&stagingDesc, nullptr, &stagingTex));
        devCtx->CopyResource(stagingTex, srcTex.get());

        // 8. 映射内存拷贝像素
        D3D11_MAPPED_SUBRESOURCE mapData{};
        check_hresult(devCtx->Map(stagingTex, 0, D3D11_MAP_READ, 0, &mapData));

        int lineByte = width * 4;
        const uint8_t* src = static_cast<const uint8_t*>(mapData.pData);
        for (int y = 0; y < height; ++y)
        {
            memcpy(buf + y * lineByte, src + y * mapData.RowPitch, lineByte);
        }
        devCtx->Unmap(stagingTex, 0);

        cleanup();
        return 1;
    }
    catch (...)
    {
        cleanup();
        return 0;
    }
}