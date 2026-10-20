#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static int has_async_marker(HMODULE module) {
    const unsigned char *base=(const unsigned char*)module;
    const IMAGE_DOS_HEADER *dos=(const IMAGE_DOS_HEADER*)base;
    const IMAGE_NT_HEADERS *nt=(const IMAGE_NT_HEADERS*)(base+dos->e_lfanew);
    const IMAGE_SECTION_HEADER *sections=IMAGE_FIRST_SECTION(nt);
    const char marker[]="DXMT_ASYNC_PSO";
    for (unsigned i=0;i<nt->FileHeader.NumberOfSections;i++) {
        SIZE_T size=sections[i].Misc.VirtualSize, read=0;
        if (!(sections[i].Characteristics&IMAGE_SCN_MEM_READ) || size<sizeof(marker) || size>64*1024*1024) continue;
        unsigned char *bytes=(unsigned char*)malloc(size);
        if (!bytes) return 0;
        int found=0;
        if (ReadProcessMemory(GetCurrentProcess(),base+sections[i].VirtualAddress,bytes,size,&read)) {
            for (SIZE_T n=0;n+sizeof(marker)<=read;n++) if (!memcmp(bytes+n,marker,sizeof(marker))) { found=1;break; }
        }
        free(bytes);if (found) return 1;
    }
    return 0;
}
int main(void) {
    const char *async=getenv("DXMT_ASYNC_PSO"), *prewarm=getenv("DXMT_PREWARM");
    if (!async || strcmp(async,"1") || !prewarm || strcmp(prewarm,"1")) {
        fputs("Async environment is not enabled\n",stderr); return 6;
    }
    HMODULE dll = LoadLibraryW(L"d3d11.dll");
    if (!dll) { fprintf(stderr,"d3d11 load error %lu\n",GetLastError()); return 2; }
    if (!has_async_marker(dll)) { fputs("Loaded d3d11 is not the Shader Warmer Async build\n",stderr); return 5; }
    typedef HRESULT (WINAPI *CreateDevice)(IDXGIAdapter*,D3D_DRIVER_TYPE,HMODULE,UINT,const D3D_FEATURE_LEVEL*,UINT,UINT,ID3D11Device**,D3D_FEATURE_LEVEL*,ID3D11DeviceContext**);
    CreateDevice create=(CreateDevice)GetProcAddress(dll,"D3D11CreateDevice");
    if (!create) return 3;
    ID3D11Device *device=NULL; ID3D11DeviceContext *context=NULL; D3D_FEATURE_LEVEL level;
    HRESULT hr=create(NULL,D3D_DRIVER_TYPE_HARDWARE,NULL,0,NULL,0,D3D11_SDK_VERSION,&device,&level,&context);
    if (FAILED(hr)) { fprintf(stderr,"D3D11CreateDevice: 0x%08lx\n",(unsigned long)hr); return 4; }
    ID3D11DeviceContext_Release(context); ID3D11Device_Release(device);
    puts("SHADER_WARMER_DXMT_OK"); return 0;
}
