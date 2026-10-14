export class ApiError extends Error {
  constructor(message, status = 0) { super(message); this.name = 'ApiError'; this.status = status; }
}

export function detailMessage(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map(item => item.msg || '请求参数有误').join('；');
  return '请求失败，请稍后重试。';
}

export class SymbolAPI {
  constructor({ getToken, onUnauthorized = () => {}, fetchImpl = globalThis.fetch.bind(globalThis) }) {
    this.getToken = getToken;
    this.onUnauthorized = onUnauthorized;
    this.fetch = fetchImpl;
  }

  async request(path, { method = 'GET', body, authenticated = true, raw = false, timeout = 30000 } = {}) {
    const headers = { Accept: raw ? '*/*' : 'application/json' };
    let requestToken;
    if (authenticated) {
      requestToken = this.getToken();
      if (!requestToken) throw new ApiError('请先登录工作区。', 401);
      headers.Authorization = `Bearer ${requestToken}`;
    }
    if (body !== undefined && !(body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await this.fetch(path, { method, headers, body, signal: controller.signal, credentials: 'same-origin' });
      if (!response.ok) {
        let detail;
        try { detail = (await response.json()).detail; } catch { /* non-JSON server error */ }
        if (response.status === 401 && authenticated) {
          if (this.getToken() === requestToken) this.onUnauthorized();
          throw new ApiError('登录已过期，请重新登录。', 401);
        }
        throw new ApiError(response.status === 405 ? '当前服务尚未加载此操作，请刷新页面或更新后端后重试。' : detailMessage(detail), response.status);
      }
      return raw ? response : await response.json();
    } catch (error) {
      if (error.name === 'AbortError') throw new ApiError('请求超时，请检查连接后重试。');
      if (error instanceof ApiError) throw error;
      throw new ApiError('暂时无法连接服务，请稍后重试。');
    } finally { clearTimeout(timer); }
  }

  login(username, password) { return this.request('/api/v1/auth/login', { method: 'POST', body: { username, password }, authenticated: false }); }
  me() { return this.request('/api/v1/auth/me'); }
  list(limit = 20, offset = 0, deleted = false) { return this.request(`/api/v1/symbol/drawings?limit=${limit}&offset=${offset}${deleted ? '&deleted=true' : ''}`); }
  get(id) { return this.request(`/api/v1/symbol/drawings/${encodeURIComponent(id)}`); }
  remove(id) { return this.request(`/api/v1/symbol/drawings/${encodeURIComponent(id)}`, { method: 'DELETE' }); }
  restore(id) { return this.request(`/api/v1/symbol/drawings/${encodeURIComponent(id)}/restore`, { method: 'POST' }); }
  trace(id) { return this.request(`/api/v1/symbol/drawings/${encodeURIComponent(id)}/trace`, { timeout: 120000 }); }
  generate(formData) { return this.request('/api/v1/symbol/generate', { method: 'POST', body: formData, timeout: 120000 }); }
  answer(id, interruptId, answer) { return this.request(`/api/v1/symbol/drawings/${encodeURIComponent(id)}/human-input`, { method: 'POST', body: { interrupt_id: interruptId, answer } }); }

  async download(id, name, fallbackName) {
    const names = new Set(['olb', 'dsn', 'tcl', 'capture_log', 'source']);
    if (!names.has(name)) throw new ApiError('未知文件类型。');
    const suffix = name === 'source' ? 'source' : `artifacts/${name}`;
    const response = await this.request(`/api/v1/symbol/drawings/${encodeURIComponent(id)}/${suffix}`, { raw: true, timeout: 120000 });
    const disposition = response.headers.get('content-disposition') || '';
    let filename = fallbackName;
    const encoded = disposition.match(/filename\*=utf-8''([^;]+)/i);
    const plain = disposition.match(/filename="([^"\r\n]+)"/i);
    if (encoded) { try { filename = decodeURIComponent(encoded[1]); } catch { /* fallback */ } }
    else if (plain) filename = plain[1];
    filename = (filename || 'download').split(/[\\/]/).pop();
    return { blob: await response.blob(), filename };
  }
}
