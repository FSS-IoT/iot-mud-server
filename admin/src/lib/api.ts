export const TOKEN_STORAGE_KEY = 'mud-admin-token';

export type HttpMethod = 'GET' | 'POST';

export class ApiError extends Error {
  status: number;
  payload: Record<string, unknown>;

  constructor(
    status: number,
    message: string,
    payload: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

function messageFrom(payload: Record<string, unknown>, status: number): string {
  const error = payload.error;
  if (typeof error === 'string' && error.trim()) return error;
  const errors = payload.errors;
  if (Array.isArray(errors) && errors.length) {
    return errors.map((item) => String(item)).join('\n');
  }
  return `Request failed with HTTP ${status}`;
}

export async function api<T>(
  token: string,
  method: HttpMethod,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
  };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin',
  });
  const text = await response.text();
  let payload: Record<string, unknown> = {};
  if (text) {
    try {
      payload = JSON.parse(text) as Record<string, unknown>;
    } catch {
      payload = { error: text };
    }
  }
  if (!response.ok) {
    throw new ApiError(
      response.status,
      messageFrom(payload, response.status),
      payload,
    );
  }
  return payload as T;
}

export function errorText(caught: unknown): string {
  if (caught instanceof ApiError || caught instanceof Error) {
    return caught.message;
  }
  return String(caught);
}

export function errorList(caught: unknown): string[] {
  if (caught instanceof ApiError) {
    const errors = caught.payload.errors;
    if (Array.isArray(errors)) return errors.map((item) => String(item));
    return [caught.message];
  }
  return [errorText(caught)];
}
