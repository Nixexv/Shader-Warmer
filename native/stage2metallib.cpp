// Offline DXIL -> metallib, replicating D3DMetal's D3DMStageCache compile path.
//
// Calls the private libmetalirconverter bundled in
// D3DMetal.framework/Versions/A/Resources exactly the way
//   D3DMCompiler::CreateIRCompiler            (GPTK 3.0.3 @0x54bea)
//   D3DMStageCache::CompileFragment::$_0      (@0x5ef20)
//   D3DMStageCache::CompileVertex::$_0        (@0x5c7ce)
//   D3DMStageCache::CompileAndLinkStage       (@0x54df6)
// do, then writes the metallib bytes.
//
// x86_64 only (the dylib is x86_64):
//   clang++ -arch x86_64 -std=c++17 -O2 stage2metallib.cpp -o stage2metallib \
//       -L"$RES" -lmetalirconverter -Wl,-rpath,"$RES"
//
// Usage: stage2metallib <in.dxil> <out.metallib> <stage:1=vs,2=ps> <shaderIDString> [key=value ...]

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <vector>

struct IRCompiler;
struct IRObject;
struct IRError;
struct IRMetalLibBinary;
struct IRRootSignature;
enum DXIL2AIRYieldMode : int {};

extern "C" {
IRCompiler* IRCompilerCreate(void);
void IRCompilerDestroy(IRCompiler*);
void IRCompilerSetCompatibilityFlags(IRCompiler*, uint64_t);
void IRCompilerSetStageInGenerationMode(IRCompiler*, int);
void IRCompilerSetValidationFlags(IRCompiler*, int);
void IRCompilerSetMinimumGPUFamily(IRCompiler*, int);
void IRCompilerIgnoreRootSignature(IRCompiler*, bool);
void IRCompilerIgnoreDebugInformation(IRCompiler*, bool);
void IRCompilerSetGlobalRootSignature(IRCompiler*, IRRootSignature*);
void IRCompilerSetDualSourceBlendingConfiguration(IRCompiler*, int);
void IRCompilerSetDepthFeedbackConfiguration(IRCompiler*, int);
void IRCompilerSetIntRTMask(IRCompiler*, uint8_t);
void IRCompilerSetSampleMask(IRCompiler*, uint32_t);
void IRCompilerSetStreamOutEnabled(IRCompiler*, bool);
void IRCompilerSetStripCutIndex(IRCompiler*, int);
void IRCompilerSetInputTopology(IRCompiler*, int);
void IRCompilerEnableGeometryAndTessellationEmulation(IRCompiler*, bool);
void IRCompilerSetTessellationEnabled(IRCompiler*, bool);
void IRCompilerSetVertexViewportIndexID(IRCompiler*, int);
void IRCompilerSetVertexRenderTargetIndexID(IRCompiler*, int);
int  IRObjectGetMetalIRShaderStage(IRObject*);
bool IRObjectGetMetalLibBinary(IRObject*, int, IRMetalLibBinary*);
void IRObjectDestroy(IRObject*);
IRMetalLibBinary* IRMetalLibBinaryCreate(void);
void IRMetalLibBinaryDestroy(IRMetalLibBinary*);
size_t IRMetalLibGetBytecodeSize(IRMetalLibBinary*);
size_t IRMetalLibGetBytecode(IRMetalLibBinary*, uint8_t*);
int IRErrorGetCode(IRError*);
struct IRShaderReflection* IRShaderReflectionCreate(void);
void IRShaderReflectionDestroy(struct IRShaderReflection*);
bool IRObjectGetReflection(IRObject*, int, struct IRShaderReflection*);
}
struct IRShaderReflection;
unsigned long long IRShaderReflectionGetBinarySize(IRShaderReflection*);
void IRShaderReflectionSerializeToBinary(IRShaderReflection*, void*, unsigned long long);

// Private C++ entry points (exported mangled from the bundled dylib).
IRObject* IRObjectCreateFromDXIL(const char*, unsigned long);
IRObject* IRCompilerAllocCompileAndLink(IRCompiler*, const std::vector<std::string>&, const IRObject*, IRError**);
void IRCompilerSetShaderID(IRCompiler*, const std::string&);
void IRCompilerSetTargetTriple(IRCompiler*, const char*);
void IRCompilerEnableYield(IRCompiler*, bool, DXIL2AIRYieldMode, unsigned short, unsigned short);
void IRCompilerSetUnorm8RTMask(IRCompiler*, unsigned char);
void IRCompilerEnableGeometryDebugging(IRCompiler*, bool);
void IRCompilerForce4WideVaryings(IRCompiler*, bool);
void IRCompilerUnsafePositionInvariance(IRCompiler*, bool);
void IRCompilerSupportRGB32TypedBuffers(IRCompiler*, bool);
void IRCompilerFlushFragmentColorNanToZero(IRCompiler*, bool);
void IRCompilerSupportUnAlignedTypedBuffers(IRCompiler*, bool);
void IRCompilerPatchFloatToUnorm(IRCompiler*, bool);
void IRCompilerSupportGloballyCoherent(IRCompiler*, bool);
void IRCompilerEnable3DViewZSlices(IRCompiler*, bool);
void IRCompilerEnableShaderDetection(IRCompiler*, bool);
void IRCompilerEnableGOSPayloadReadWA(IRCompiler*, bool);
void IRCompilerEnableFeedback(IRCompiler*, bool);

static std::vector<char> readAll(const char* p) {
    std::vector<char> v;
    FILE* f = fopen(p, "rb");
    if (!f) return v;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    v.resize(n);
    if (n > 0 && fread(v.data(), 1, n, f) != (size_t)n) v.clear();
    fclose(f);
    return v;
}

static int compileOne(const std::map<std::string, std::string>& kv, const char* inPath,
                      const char* outPath, const char* reflPath, int stage, const std::string& sid) {
    auto num = [&](const char* k, unsigned long long def) -> unsigned long long {
        auto it = kv.find(k);
        return it == kv.end() ? def : strtoull(it->second.c_str(), nullptr, 0);
    };
    auto str = [&](const char* k, const char* def) -> std::string {
        auto it = kv.find(k);
        return it == kv.end() ? def : it->second;
    };
    std::vector<char> dxil = readAll(inPath);
    if (dxil.empty()) { fprintf(stderr, "cannot read %s\n", inPath); return 2; }


    // --- D3DMCompiler::CreateIRCompiler, call-for-call ---
    IRCompiler* c = IRCompilerCreate();
    IRCompilerSetCompatibilityFlags(c, num("compat", 0));
    IRCompilerEnableGeometryDebugging(c, num("geomdbg", 0));
    IRCompilerForce4WideVaryings(c, num("force4wide", 0));
    IRCompilerUnsafePositionInvariance(c, num("unsafepos", 0));
    IRCompilerSupportRGB32TypedBuffers(c, num("rgb32", 0));
    IRCompilerFlushFragmentColorNanToZero(c, num("flushnan", 0));
    IRCompilerSupportUnAlignedTypedBuffers(c, num("unaligned", 0));
    IRCompilerPatchFloatToUnorm(c, num("patchfloat", 0));
    IRCompilerSupportGloballyCoherent(c, true);
    IRCompilerEnable3DViewZSlices(c, false);
    IRCompilerEnableShaderDetection(c, num("shaderdetect", 0));
    IRCompilerEnableYield(c, num("yield", 0), (DXIL2AIRYieldMode)num("yieldmode", 0),
                          (unsigned short)num("yielda", 0), (unsigned short)num("yieldb", 0));
    IRCompilerEnableGOSPayloadReadWA(c, num("gos", 0));
    IRCompilerSetStageInGenerationMode(c, (int)num("stagein", 1));
    IRCompilerSetShaderID(c, sid);
    IRCompilerSetValidationFlags(c, 0);
    std::string triple = str("triple", "air64-apple-macosx26.0.0");
    IRCompilerSetTargetTriple(c, triple.c_str());
    IRCompilerSetMinimumGPUFamily(c, (int)num("gpufamily", 0));
    IRCompilerIgnoreRootSignature(c, num("ignorers", 0));
    IRCompilerIgnoreDebugInformation(c, num("ignoredbg", 1));
    if (kv.count("feedback")) IRCompilerEnableFeedback(c, num("feedback", 0));

    // --- per-stage setters ---
    if (stage == 2) {
        IRCompilerSetDualSourceBlendingConfiguration(c, 2 - (int)num("dsb", 0));
        IRCompilerSetDepthFeedbackConfiguration(c, 2 - (int)num("depthfb", 0));
        IRCompilerSetIntRTMask(c, (uint8_t)num("intrt", 0));
        IRCompilerSetUnorm8RTMask(c, (unsigned char)num("unorm8", 0));
        IRCompilerSetSampleMask(c, (uint32_t)num("samplemask", 0xffffffff));
    } else if (stage == 1) {
        IRCompilerSetStreamOutEnabled(c, num("streamout", 0));
        IRCompilerSetStripCutIndex(c, (int)num("stripcut", 0));
        if (num("pointtopo", 0) == 1) IRCompilerSetInputTopology(c, 1);
        IRCompilerEnableGeometryAndTessellationEmulation(c, num("gstsemu", 0));
        IRCompilerSetTessellationEnabled(c, num("tess", 0));
        IRCompilerSetVertexViewportIndexID(c, (int)(long long)num("vpindex", (unsigned long long)-1));
        IRCompilerSetVertexRenderTargetIndexID(c, (int)(long long)num("rtindex", (unsigned long long)-1));
    }

    // --- D3DMStageCache::CompileAndLinkStage ---
    IRObject* in = IRObjectCreateFromDXIL(dxil.data(), dxil.size());
    IRCompilerSetGlobalRootSignature(c, nullptr);
    IRError* err = nullptr;
    std::vector<std::string> entryPoints;
    IRObject* out = IRCompilerAllocCompileAndLink(c, entryPoints, in, &err);
    if (!out) {
        fprintf(stderr, "compile failed: code=%d\n", err ? IRErrorGetCode(err) : -1);
        { IRCompilerDestroy(c); return 1; }
    }
    int got = IRObjectGetMetalIRShaderStage(out);
    if (got != stage) fprintf(stderr, "warning: stage %d != requested %d\n", got, stage);

    IRMetalLibBinary* lib = IRMetalLibBinaryCreate();
    if (!IRObjectGetMetalLibBinary(out, stage, lib)) { fprintf(stderr, "GetMetalLibBinary failed\n"); { IRCompilerDestroy(c); return 1; } }
    size_t n = IRMetalLibGetBytecodeSize(lib);
    std::vector<uint8_t> bytes(n);
    IRMetalLibGetBytecode(lib, bytes.data());
    FILE* o = fopen(outPath, "wb");
    fwrite(bytes.data(), 1, n, o);
    fclose(o);



    if (reflPath && *reflPath && strcmp(reflPath, "-") != 0) {
        IRShaderReflection* refl = IRShaderReflectionCreate();
        if (IRObjectGetReflection(out, stage, refl)) {
            unsigned long long rn = IRShaderReflectionGetBinarySize(refl);
            std::vector<uint8_t> rb(rn);
            IRShaderReflectionSerializeToBinary(refl, rb.data(), rn);
            FILE* rf = fopen(reflPath, "wb");
            if (rf) { fwrite(rb.data(), 1, rn, rf); fclose(rf); }
        }
        IRShaderReflectionDestroy(refl);
    }
    IRMetalLibBinaryDestroy(lib);
    IRObjectDestroy(out);
    IRObjectDestroy(in);
    IRCompilerDestroy(c);
    return 0;
}

static std::map<std::string, std::string> parseKV(char** begin, char** end) {
    std::map<std::string, std::string> kv;
    for (char** a = begin; a < end; a++) {
        const char* eq = strchr(*a, '=');
        if (eq) kv[std::string(*a, eq - *a)] = eq + 1;
    }
    return kv;
}

// Single: stage2metallib <in.dxil> <out.metallib> <stage> <sid> [k=v...]
// Batch:  stage2metallib --batch <jobs.tsv> [global k=v...]
//   jobs.tsv: in.dxil \t out.metallib \t out.refl|- \t stage \t k=v,k=v,...
int main(int argc, char** argv) {
    if (argc >= 3 && strcmp(argv[1], "--batch") == 0) {
        auto global = parseKV(argv + 3, argv + argc);
        FILE* f = fopen(argv[2], "r");
        if (!f) { fprintf(stderr, "cannot read %s\n", argv[2]); return 66; }
        char line[16384];
        size_t ok = 0, fail = 0;
        while (fgets(line, sizeof line, f)) {
            std::vector<std::string> col;
            char* save = nullptr;
            for (char* t = strtok_r(line, "\t\r\n", &save); t; t = strtok_r(nullptr, "\t\r\n", &save)) col.push_back(t);
            if (col.size() < 4) continue;
            auto kv = global;
            if (col.size() >= 5) {
                std::string ov = col[4];
                size_t p = 0;
                while (p <= ov.size()) {
                    size_t q = ov.find(',', p);
                    std::string item = ov.substr(p, q == std::string::npos ? std::string::npos : q - p);
                    size_t eq = item.find('=');
                    if (eq != std::string::npos) kv[item.substr(0, eq)] = item.substr(eq + 1);
                    if (q == std::string::npos) break;
                    p = q + 1;
                }
            }
            auto sidIt = kv.find("sid");
            // The ShaderID string is embedded in the serialized reflection (not in the metallib).
            int rc = compileOne(kv, col[0].c_str(), col[1].c_str(), col[2].c_str(), atoi(col[3].c_str()),
                                sidIt == kv.end() ? std::string("0") : sidIt->second);
            if (rc) { fprintf(stderr, "FAIL %s rc=%d\n", col[0].c_str(), rc); fail++; } else ok++;
        }
        fclose(f);
        printf("compiled=%zu failed=%zu\n", ok, fail);
        return fail ? 1 : 0;
    }
    if (argc < 5) {
        fprintf(stderr, "usage: %s <in.dxil> <out.metallib> <stage 1|2> <shaderIDString> [k=v...]\n"
                        "       %s --batch <jobs.tsv> [k=v...]\n", argv[0], argv[0]);
        return 64;
    }
    auto kv = parseKV(argv + 5, argv + argc);
    auto it = kv.find("refl");
    return compileOne(kv, argv[1], argv[2], it == kv.end() ? nullptr : it->second.c_str(), atoi(argv[3]), argv[4]);
}
