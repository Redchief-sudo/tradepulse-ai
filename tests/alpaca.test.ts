import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { getAlpacaAccount, placeAlpacaOrder } from '../base44/shared/alpaca.ts';

const creds = { apiKey: 'key', secretKey: 'secret', mode: 'paper' };

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

describe('alpaca.ts — retry/backoff (H6)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('retries a 500 twice then succeeds on the third attempt', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(500, { message: 'server error' }))
      .mockResolvedValueOnce(jsonResponse(500, { message: 'server error' }))
      .mockResolvedValueOnce(jsonResponse(200, { equity: '100000', cash: '50000' }));
    vi.stubGlobal('fetch', fetchMock);

    const promise = getAlpacaAccount(creds);
    await vi.runAllTimersAsync();
    const result = await promise;

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(result.equity).toBe('100000');
  });

  it('retries a 429 rate-limit response', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(429, { message: 'rate limited' }))
      .mockResolvedValueOnce(jsonResponse(200, { equity: '100000' }));
    vi.stubGlobal('fetch', fetchMock);

    const promise = getAlpacaAccount(creds);
    await vi.runAllTimersAsync();
    const result = await promise;

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.equity).toBe('100000');
  });

  it('does not retry a 401 auth error — fails immediately', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(401, { message: 'auth failed' }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(getAlpacaAccount(creds)).rejects.toThrow();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('throws after exhausting all attempts on persistent 500s', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(500, { message: 'server error' }));
    vi.stubGlobal('fetch', fetchMock);

    const promise = getAlpacaAccount(creds);
    const assertion = expect(promise).rejects.toThrow();
    await vi.runAllTimersAsync();
    await assertion;
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('placeAlpacaOrder is called exactly once even when failing — deliberately excluded from retry', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(500, { message: 'server error' }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      placeAlpacaOrder({ ...creds, symbol: 'AAPL', qty: 1, side: 'buy', client_order_id: 'co-1' })
    ).rejects.toThrow();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
