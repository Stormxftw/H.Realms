/** @typedef {string | number | boolean} SelectKeyValue */

const TERMINAL_STATES = new Set(['succeeded', 'failed', 'cancelled', 'outcome_unknown'])

/**
 * Format a select option value as a stable, type-qualified key.
 * @param {SelectKeyValue} value
 * @returns {string}
 */
export function selectKey(value) {
  const type = typeof value
  if (!['string', 'number', 'boolean'].includes(type)) throw new TypeError('unsupported select value')
  return `${type}:${JSON.stringify(value)}`
}

/**
 * Resolve a select option value from a type-qualified key.
 * @param {{ label?: string, value: SelectKeyValue }[]} options
 * @param {string} key
 * @returns {SelectKeyValue | undefined}
 */
export function selectValue(options, key) {
  const option = options.find(item => selectKey(item.value) === key)
  return option?.value
}

/**
 * Validate a raw numeric input against a control's min/max/step constraints.
 * @param {string} raw
 * @param {{ min?: number, max?: number, step?: number }} control
 * @returns {{ valid: boolean, value: number | undefined }}
 */
export function numericDraft(raw, { min, max, step } = {}) {
  if (typeof raw !== 'string' || raw.trim() === '') return { valid: false, value: undefined }
  const value = Number(raw)
  let valid = Number.isFinite(value)
  if (valid && min !== undefined) valid = value >= min
  if (valid && max !== undefined) valid = value <= max
  if (valid && step !== undefined && step > 0) {
    const base = min ?? 0
    const quotient = (value - base) / step
    valid = Math.abs(quotient - Math.round(quotient)) < 1e-9
  }
  return { valid, value: valid ? value : undefined }
}

/**
 * @typedef {object} PollOperation
 * @property {string} operationId
 * @property {string} state
 * @property {string | null} [output]
 * @property {string | null} [recoveryNote]
 */

/**
 * @typedef {object} PollOptions
 * @property {number} [intervalMs]
 * @property {number} [timeoutMs]
 * @property {() => number} [now]
 * @property {(ms: number) => Promise<void>} [sleep]
 * @property {AbortSignal} [signal]
 */

/**
 * Poll one durable operation until it reaches a terminal state.
 * @param {(path: string) => Promise<PollOperation>} rest
 * @param {PollOperation} initial
 * @param {PollOptions} [options]
 * @returns {Promise<PollOperation>}
 */
export async function waitForOperation(rest, initial, options = {}) {
  const intervalMs = options.intervalMs ?? 750
  const timeoutMs = options.timeoutMs ?? 300_000
  const now = options.now ?? Date.now
  const sleep = options.sleep ?? (ms => new Promise(resolve => setTimeout(resolve, ms)))
  const signal = options.signal
  let operation = initial
  const deadline = now() + timeoutMs
  while (!TERMINAL_STATES.has(operation.state)) {
    if (signal?.aborted) throw new Error('Operation polling cancelled.')
    if (now() >= deadline) throw new Error(`Operation ${operation.operationId} polling timed out.`)
    await sleep(Math.min(intervalMs, Math.max(0, deadline - now())))
    if (signal?.aborted) throw new Error('Operation polling cancelled.')
    operation = await rest(`/proxy/api/operations/${encodeURIComponent(operation.operationId)}`)
  }
  return operation
}

/**
 * Build a human-readable message for a terminal operation.
 * @param {PollOperation} operation
 * @returns {string}
 */
export function operationMessage(operation) {
  return operation.output || operation.recoveryNote || `Operation ${operation.state}.`
}

/**
 * @param {PollOperation} operation
 * @returns {boolean}
 */
export function operationSucceeded(operation) {
  return operation.state === 'succeeded'
}