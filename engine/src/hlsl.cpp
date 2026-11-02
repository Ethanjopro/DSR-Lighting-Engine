#include "hlsl.h"
#include "common.h"
#include <d3dcompiler.h>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

using D3DCompileFn = decltype(&D3DCompile);

ID3D11Device* g_device;
D3DCompileFn g_compile;
HMODULE g_compiler;
std::mutex g_lock;  // requests come from the game's recording threads
std::unordered_map<std::string, ID3D11DeviceChild*> g_cache;
// Shaders replaced by a reload may still be in a recording thread's hands, so they are released a few
// frames later. Once bound, a context holds its own reference.
struct Retired { ID3D11DeviceChild* shader; int frames_left; };
std::vector<Retired> g_retired;
uint64_t g_stamp;
int g_poll_counter;

std::wstring widen(const std::string& s) { return std::wstring(s.begin(), s.end()); }

bool read_file(const std::wstring& path, std::string& out) {
    FILE* f = nullptr;
    _wfopen_s(&f, path.c_str(), L"rb");
    if (!f) return false;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    out.resize(n);
    if (n) fread(out.data(), 1, n, f);
    fclose(f);
    return true;
}

// Includes resolve against dsrle\hlsl only.
class Includes final : public ID3DInclude {
public:
    HRESULT STDMETHODCALLTYPE Open(D3D_INCLUDE_TYPE, LPCSTR name, LPCVOID, LPCVOID* data, UINT* bytes) override {
        std::string text;
        if (!read_file(data_dir() + L"hlsl\\" + widen(name), text)) return E_FAIL;
        char* copy = new char[text.size()];
        memcpy(copy, text.data(), text.size());
        *data = copy;
        *bytes = static_cast<UINT>(text.size());
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE Close(LPCVOID data) override {
        delete[] static_cast<const char*>(data);
        return S_OK;
    }
};

uint64_t folder_stamp() {
    uint64_t newest = 0;
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileW((data_dir() + L"hlsl\\*").c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) return 0;
    do {
        // ".." is the dsrle folder, whose timestamp changes whenever a trigger or the ini is written.
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) continue;
        uint64_t t = (static_cast<uint64_t>(fd.ftLastWriteTime.dwHighDateTime) << 32) | fd.ftLastWriteTime.dwLowDateTime;
        if (t > newest) newest = t;
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return newest;
}

ID3DBlob* compile(const char* file, const char* entry, const char* target, const std::vector<ShaderDefine>& defines) {
    if (!g_compile) return nullptr;
    std::string source;
    if (!read_file(data_dir() + L"hlsl\\" + widen(file), source)) {
        logf("hlsl: cannot read %s", file);
        return nullptr;
    }
    std::vector<D3D_SHADER_MACRO> macros;
    for (const auto& d : defines) macros.push_back({d.name, d.value});
    macros.push_back({nullptr, nullptr});
    Includes includes;
    ID3DBlob* code = nullptr;
    ID3DBlob* errors = nullptr;
    HRESULT hr = g_compile(source.data(), source.size(), file, macros.data(), &includes, entry, target, D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &code, &errors);
    if (errors) {
        logf("hlsl: %s %s:\n%s", file, entry, static_cast<const char*>(errors->GetBufferPointer()));
        errors->Release();
    }
    if (FAILED(hr)) {
        if (code) code->Release();
        return nullptr;
    }
    logf("hlsl: compiled %s %s", file, entry);
    return code;
}

std::string cache_key(const char* file, const char* entry, const char* target, const std::vector<ShaderDefine>& defines) {
    std::string key = std::string(file) + "|" + entry + "|" + target;
    for (const auto& d : defines) key += std::string("|") + d.name + "=" + (d.value ? d.value : "");
    return key;
}

template <class T, class Create>
T* get_shader(const char* file, const char* entry, const char* target, const std::vector<ShaderDefine>& defines, Create create) {
    const std::string key = cache_key(file, entry, target, defines);
    std::lock_guard<std::mutex> lock(g_lock);
    auto it = g_cache.find(key);
    if (it != g_cache.end()) return static_cast<T*>(it->second);
    ID3DBlob* code = compile(file, entry, target, defines);
    T* shader = nullptr;
    if (code) {
        create(code, &shader);
        code->Release();
    }
    // Failures are cached too: retrying a broken shader every frame would stall the game.
    g_cache[key] = shader;
    return shader;
}

}  // namespace

void hlsl_init(ID3D11Device* device) {
    g_device = device;
    wchar_t path[MAX_PATH];
    GetSystemDirectoryW(path, MAX_PATH);
    wcscat_s(path, L"\\d3dcompiler_47.dll");
    g_compiler = LoadLibraryW(path);
    if (g_compiler) g_compile = reinterpret_cast<D3DCompileFn>(GetProcAddress(g_compiler, "D3DCompile"));
    logf("hlsl: compiler %s", g_compile ? "loaded" : "NOT available");
    g_stamp = folder_stamp();
}

bool hlsl_poll() {
    std::lock_guard<std::mutex> lock(g_lock);
    for (size_t i = 0; i < g_retired.size();) {
        if (--g_retired[i].frames_left <= 0) {
            g_retired[i].shader->Release();
            g_retired[i] = g_retired.back();
            g_retired.pop_back();
        } else {
            i++;
        }
    }
    if (++g_poll_counter % 30) return false;
    uint64_t now = folder_stamp();
    if (now == g_stamp) return false;
    g_stamp = now;
    for (auto& kv : g_cache) {
        if (kv.second) g_retired.push_back({kv.second, 8});
    }
    g_cache.clear();
    logf("hlsl: sources changed, cache cleared");
    return true;
}

ID3D11ComputeShader* hlsl_cs(const char* file, const char* entry, const std::vector<ShaderDefine>& defines) {
    return get_shader<ID3D11ComputeShader>(file, entry, "cs_5_0", defines, [](ID3DBlob* c, ID3D11ComputeShader** out) {
        g_device->CreateComputeShader(c->GetBufferPointer(), c->GetBufferSize(), nullptr, out);
    });
}

ID3D11PixelShader* hlsl_ps(const char* file, const char* entry, const std::vector<ShaderDefine>& defines) {
    return get_shader<ID3D11PixelShader>(file, entry, "ps_5_0", defines, [](ID3DBlob* c, ID3D11PixelShader** out) {
        g_device->CreatePixelShader(c->GetBufferPointer(), c->GetBufferSize(), nullptr, out);
    });
}

ID3D11VertexShader* hlsl_vs(const char* file, const char* entry, const std::vector<ShaderDefine>& defines) {
    return get_shader<ID3D11VertexShader>(file, entry, "vs_5_0", defines, [](ID3DBlob* c, ID3D11VertexShader** out) {
        g_device->CreateVertexShader(c->GetBufferPointer(), c->GetBufferSize(), nullptr, out);
    });
}
