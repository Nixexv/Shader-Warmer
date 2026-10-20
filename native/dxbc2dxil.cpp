// Offline SM5 DXBC -> DXIL container converter.
//
// Calls hlsl::DxbcConverter (Microsoft "dxbc2dxil 1.2") directly out of the
// libdxilconv.dylib that ships inside D3DMetal.framework/Versions/A/Resources.
// This is the exact code path D3DMetal uses via
// DXCompiler::DXBCToDXIL(ShaderBytecode, ShaderBytecode&) in libdxccontainer,
// so the output is byte-identical to what D3DMetal writes into
// bytecode_cache.bin (verified 12/12 against final_all_shader_cache_151722_gptk303).
//
// The dylibs are x86_64 only -> build for x86_64 and run under Rosetta:
//   clang++ -arch x86_64 -std=c++17 -fno-rtti -O2 -o dxbc2dxil dxbc2dxil.cpp
//   arch -x86_64 ./dxbc2dxil <resources_dir> <jobs.tsv>
// jobs.tsv: one "<in_dxbc_path>\t<out_dxil_path>" per line. One process handles
// the whole batch (converter is created once; Rosetta start-up is paid once).
//
// Single-file mode: arch -x86_64 ./dxbc2dxil <resources_dir> -1 <in> <out>

#include <dlfcn.h>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

typedef int32_t HRESULT_;
struct GUID_ { uint32_t a; uint16_t b, c; uint8_t d[8]; };

struct IUnknownX {
    virtual HRESULT_ QueryInterface(const GUID_&, void**) = 0;
    virtual uint32_t AddRef() = 0;
    virtual uint32_t Release() = 0;
};
struct IDxbcConverterX : IUnknownX {
    virtual HRESULT_ Convert(const void* dxbc, uint32_t dxbcSize,
                             const wchar_t* extraOptions,
                             void** outDxil, uint32_t* outSize,
                             wchar_t** outDiag) = 0;
};

// Recovered from _DxcCreateInstance / _CLSID_DxbcConverter in libdxilconv.dylib.
static const GUID_ CLSID_DxbcConverter =
    {0x4900391E, 0xB752, 0x4EDD, {0xA8,0x85,0x6F,0xB7,0x6E,0x25,0xAD,0xDB}};
// D3DMetal's DXCompiler::DXBCToDXIL requests this IID (the one whose first
// virtual slot is Convert). ECC8691B-... also resolves but is a different vtable.
static const GUID_ IID_IDxbcConverter =
    {0x5F956ED5, 0x78D1, 0x4B15, {0x82,0x47,0xF7,0x18,0x76,0x14,0xA0,0x41}};

typedef HRESULT_ (*PfnDxcCreateInstance)(const GUID_&, const GUID_&, void**);

static bool readFile(const char* path, std::vector<uint8_t>& out) {
    FILE* f = fopen(path, "rb");
    if (!f) return false;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    out.resize(n > 0 ? (size_t)n : 0);
    bool ok = n <= 0 || fread(out.data(), 1, (size_t)n, f) == (size_t)n;
    fclose(f);
    return ok;
}

int main(int argc, char** argv) {
    if (argc < 3) {
        fprintf(stderr,
                "usage: %s <D3DMetal.framework/Versions/A/Resources> <jobs.tsv>\n"
                "       %s <resources> -1 <in.dxbc> <out.dxil>\n", argv[0], argv[0]);
        return 64;
    }
    std::string lib = std::string(argv[1]) + "/libdxilconv.dylib";
    void* h = dlopen(lib.c_str(), RTLD_NOW);
    if (!h) { fprintf(stderr, "dlopen %s: %s\n", lib.c_str(), dlerror()); return 70; }
    auto create = (PfnDxcCreateInstance)dlsym(h, "DxcCreateInstance");
    if (!create) { fprintf(stderr, "DxcCreateInstance not found\n"); return 70; }

    {   // fail fast if the interface is unavailable
        void* probe = nullptr;
        HRESULT_ hr0 = create(CLSID_DxbcConverter, IID_IDxbcConverter, &probe);
        if (hr0 != 0 || !probe) { fprintf(stderr, "CoCreate failed hr=0x%08x\n", (unsigned)hr0); return 70; }
        ((IUnknownX*)probe)->Release();
    }

    std::vector<std::pair<std::string, std::string>> jobs;
    if (strcmp(argv[2], "-1") == 0) {
        if (argc < 5) { fprintf(stderr, "-1 needs <in> <out>\n"); return 64; }
        jobs.emplace_back(argv[3], argv[4]);
    } else {
        FILE* f = fopen(argv[2], "r");
        if (!f) { fprintf(stderr, "cannot read %s\n", argv[2]); return 66; }
        char line[8192];
        while (fgets(line, sizeof line, f)) {
            char* tab = strchr(line, '\t');
            if (!tab) continue;
            *tab = 0;
            std::string in = line, out = tab + 1;
            while (!out.empty() && (out.back() == '\n' || out.back() == '\r')) out.pop_back();
            if (!in.empty() && !out.empty()) jobs.emplace_back(in, out);
        }
        fclose(f);
    }

    size_t ok = 0, fail = 0;
    std::vector<uint8_t> in;
    size_t index = 0;
    // The converter does not only reject some shaders, it hangs on a few.  Announcing each job
    // before it starts lets the caller see which one it died on and resume past it, instead of
    // bisecting a batch to find it.
    setvbuf(stdout, nullptr, _IOLBF, 0);
    for (auto& job : jobs) {
        printf("JOB %zu %s\n", index++, job.first.c_str());
        if (!readFile(job.first.c_str(), in) || in.size() < 32) {
            fprintf(stderr, "FAIL read %s\n", job.first.c_str()); fail++; continue;
        }
        // A fresh converter per shader is REQUIRED for bit-exact output: the
        // converter accumulates state across Convert calls and a reused
        // instance yields different (still valid) DXIL. D3DMetal creates and
        // releases one per conversion under a global mutex.
        void* obj = nullptr;
        HRESULT_ hr = create(CLSID_DxbcConverter, IID_IDxbcConverter, &obj);
        if (hr != 0 || !obj) { fprintf(stderr, "FAIL create for %s\n", job.first.c_str()); fail++; continue; }
        auto* cv = (IDxbcConverterX*)obj;

        void* outp = nullptr; uint32_t outn = 0; wchar_t* diag = nullptr;
        hr = cv->Convert(in.data(), (uint32_t)in.size(), nullptr, &outp, &outn, &diag);
        if (hr != 0 || !outp || !outn) {
            cv->Release();
            fprintf(stderr, "FAIL convert %s hr=0x%08x%s%ls\n", job.first.c_str(),
                    (unsigned)hr, diag ? " diag=" : "", diag ? diag : L"");
            fail++; continue;
        }
        FILE* o = fopen(job.second.c_str(), "wb");
        if (!o) { fprintf(stderr, "FAIL write %s\n", job.second.c_str()); cv->Release(); fail++; continue; }
        fwrite(outp, 1, outn, o);
        fclose(o);
        cv->Release();
        ok++;
    }
    printf("converted=%zu failed=%zu\n", ok, fail);
    return fail ? 1 : 0;
}
