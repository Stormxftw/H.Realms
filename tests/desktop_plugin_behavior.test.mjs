import assert from 'node:assert/strict'
import test from 'node:test'
import {
  numericDraft,
  selectKey,
  selectValue,
  waitForOperation,
} from '../desktop-plugin/behavior.mjs'

test('typed select keys round-trip strings, numbers, and booleans', () => {
  const options = [{ value: '1' }, { value: 1 }, { value: true }, { value: false }]
  for (const option of options) {
    assert.deepEqual(selectValue(options, selectKey(option.value)), option.value)
  }
})

test('numeric drafts reject empty, nonfinite, range and step mismatch', () => {
  assert.deepEqual(numericDraft('', { min: 0 }), { valid: false, value: undefined })
  assert.deepEqual(numericDraft('nope', {}), { valid: false, value: undefined })
  assert.equal(numericDraft('4', { min: 1, max: 5, step: 1 }).valid, true)
  assert.equal(numericDraft('0', { min: 1 }).valid, false)
  assert.equal(numericDraft('6', { max: 5 }).valid, false)
  assert.equal(numericDraft('1.5', { min: 1, step: 1 }).valid, false)
})

test('operation polling waits through queued/running and returns succeeded', async () => {
  const states = ['running', 'succeeded']
  const calls = []
  const result = await waitForOperation(
    async path => { calls.push(path); return { operationId: 'op-1', state: states.shift(), output: 'done' } },
    { operationId: 'op-1', state: 'queued' },
    { sleep: async () => {}, timeoutMs: 1000 },
  )
  assert.equal(result.state, 'succeeded')
  assert.deepEqual(calls, ['/proxy/api/operations/op-1', '/proxy/api/operations/op-1'])
})

test('operation polling returns all terminal failure states truthfully', async () => {
  for (const state of ['failed', 'cancelled', 'outcome_unknown']) {
    const result = await waitForOperation(async () => { throw new Error('must not poll') }, { operationId: 'x', state })
    assert.equal(result.state, state)
  }
})

test('operation polling times out and respects cancellation', async () => {
  let now = 0
  await assert.rejects(
    waitForOperation(async () => ({ operationId: 'x', state: 'queued' }), { operationId: 'x', state: 'queued' }, {
      now: () => now,
      sleep: async ms => { now += ms },
      timeoutMs: 10,
      intervalMs: 6,
    }),
    /timed out/i,
  )
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(
    waitForOperation(async () => ({ operationId: 'x', state: 'running' }), { operationId: 'x', state: 'queued' }, { signal: controller.signal }),
    /cancelled/i,
  )
})
