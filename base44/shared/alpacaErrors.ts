// Typed Alpaca API error — captures the HTTP status, error code, and the
// X-Request-ID header that Alpaca returns for support tracing.
// Per Alpaca docs: "Preserve the X-Request-ID returned by trading API calls
// because it is needed to trace support issues."

export class AlpacaError extends Error {
  constructor(message, statusCode, requestId, errorCode) {
    super(message);
    this.name = 'AlpacaError';
    this.statusCode = statusCode;
    this.requestId = requestId;
    this.errorCode = errorCode;
  }

  isAuthError() {
    return this.statusCode === 401 || this.statusCode === 403;
  }

  isRateLimit() {
    return this.statusCode === 429;
  }

  isInsufficientBuyingPower() {
    const msg = (this.message || '').toLowerCase();
    return this.statusCode === 403 && (msg.includes('buying power') || msg.includes('insufficient'));
  }

  isMarketClosed() {
    const msg = (this.message || '').toLowerCase();
    return msg.includes('market is closed') || msg.includes('not open');
  }

  toAuditDetail() {
    return {
      error_type: 'AlpacaError',
      status_code: this.statusCode,
      request_id: this.requestId,
      error_code: this.errorCode,
      message: this.message,
    };
  }
}

// Extract the X-Request-ID from a fetch Response headers.
export function extractRequestId(res) {
  try {
    return res.headers.get('x-request-id') || null;
  } catch (e) {
    return null;
  }
}

// Parse an Alpaca error response and throw a typed AlpacaError.
export async function throwAlpacaError(res, context) {
  const requestId = extractRequestId(res);
  let data;
  try {
    data = await res.json();
  } catch (e) {
    data = {};
  }
  const message = data?.message || data?.error || `Alpaca HTTP ${res.status} (${context})`;
  const errorCode = data?.code || null;
  throw new AlpacaError(message, res.status, requestId, errorCode);
}