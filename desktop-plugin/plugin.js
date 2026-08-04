import {
  Badge,
  Button,
  ConfirmDialog,
  ErrorState,
  GlyphSpinner,
  Input,
  PALETTE_AREA,
  ROUTES_AREA,
  ScrollArea,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Separator,
  SIDEBAR_NAV_AREA,
  Skeleton,
  StatusDot,
  Switch,
  host,
  useMutation,
  usePluginI18n,
  useQuery,
  useQueryClient,
  useValue,
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx } from 'react/jsx-runtime'

const TERMINAL_STATES = new Set(['succeeded', 'failed', 'cancelled', 'outcome_unknown'])

/**
 * Format a select option value as a stable, type-qualified key.
 * @param {string | number | boolean} value
 * @returns {string}
 */
function selectKey(value) {
  const type = typeof value
  if (!['string', 'number', 'boolean'].includes(type)) throw new TypeError('unsupported select value')
  return `${type}:${JSON.stringify(value)}`
}

/**
 * Resolve a select option value from a type-qualified key.
 * @param {{ label?: string, value: string | number | boolean }[]} options
 * @param {string} key
 * @returns {string | number | boolean | undefined}
 */
function selectValue(options, key) {
  const option = options.find(item => selectKey(item.value) === key)
  return option?.value
}

/**
 * Validate a raw numeric input against a control's min/max/step constraints.
 * @param {string} raw
 * @param {{ min?: number, max?: number, step?: number }} control
 * @returns {{ valid: boolean, value: number | undefined }}
 */
function numericDraft(raw, { min, max, step } = {}) {
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
async function waitForOperation(rest, initial, options = {}) {
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
function operationMessage(operation) {
  return operation.output || operation.recoveryNote || `Operation ${operation.state}.`
}

/**
 * @param {PollOperation} operation
 * @returns {boolean}
 */
function operationSucceeded(operation) {
  return operation.state === 'succeeded'
}

/** @typedef {string | number | boolean | null | undefined} ControlValue */
/** @typedef {{ action?: string }} ControlBinding */
/** @typedef {{ label: string, value: string | number | boolean }} ControlOption */
/**
 * @typedef {object} Control
 * @property {string} id
 * @property {string} label
 * @property {string} kind
 * @property {string} [help]
 * @property {string} [risk]
 * @property {boolean} [disabled]
 * @property {string} [disabledReason]
 * @property {string} [enabledWhen]
 * @property {ControlBinding} [binding]
 * @property {*} [variant]
 * @property {number} [max]
 * @property {number} [min]
 * @property {number} [step]
 * @property {number} [maxLength]
 * @property {string} [unit]
 * @property {boolean} [restartRequired]
 * @property {ControlOption[]} [options]
 * @property {string} [group]
 * @property {ControlValue} [value]
 */
/**
 * @typedef {object} Game
 * @property {string} id
 * @property {string} name
 * @property {string} [description]
 * @property {string} [readiness]
 * @property {boolean} [projectPresent]
 * @property {boolean} [installed]
 * @property {{ message?: string }[]} [blockers]
 * @property {{ message?: string }[]} [hints]
 * @property {Control[]} [controls]
 */
/** @typedef {{ games?: Game[] }} Catalog */
/**
 * @typedef {object} ServiceStatus
 * @property {boolean} [online]
 * @property {string} [state]
 * @property {{ ok?: boolean, running?: boolean, error?: string, uptimeHuman?: string, rssMB?: number }} [process]
 * @property {{ lan?: string, local?: string, public?: string }} [connect]
 * @property {{ online?: number, max?: number }} [players]
 * @property {{ ok?: boolean, listening?: boolean, error?: string, protocol?: string, port?: number }[]} [listeners]
 * @property {{ attempted?: boolean, ok?: boolean, error?: string }} [query]
 */
/** @typedef {{ services?: Record<string, ServiceStatus> }} HostStatus */
/**
 * @typedef {object} ActionPlan
 * @property {string} planId
 * @property {string} planDigest
 * @property {string} gameId
 * @property {string} gameName
 * @property {string} controlId
 * @property {string} controlLabel
 * @property {string} risk
 * @property {ControlValue} currentValue
 * @property {ControlValue} proposedValue
 * @property {boolean} restartRequired
 */
/** @typedef {{ gameId: string, controlId: string, value: ControlValue }} PlanRequest */
/** @typedef {{ operationId: string, state: string, output?: string, recoveryNote?: string }} OperationRecord */
/** @typedef {{ label: string, detail: string, at: string }} ActivityItem */
/** @typedef {{ artifactId: string, filename?: string, sizeBytes?: number, validation?: { state?: string, entryCount?: number } }} BackupArtifact */
/** @typedef {{ previewId: string, artifactId: string, archiveEntries?: string[], requiredConfirmation: string }} RestorePreview */
/** @typedef {{ state?: string, logId?: string, content?: string }} DiagnosticTail */
/** @typedef {{ gameId: string, mediaType: string, dataUrl: string, objectPosition?: string, attribution?: string, licenseSpdx?: string }} GameArt */
/** @typedef {{ gameId: string, artifactId: string, serverState: string }} RestorePreviewRequest */
/** @typedef {{ gameId: string, previewId: string, confirmation: string, serverState: string }} RestoreExecuteRequest */

const ID = 'game-host-console'
const ROUTE = '/game-host'
const ACTOR = 'hermes-desktop'

const panelStyle = {
  border: '1px solid var(--ui-stroke-secondary)',
  borderRadius: '8px',
}

/** @param {string | null | undefined} risk */
function riskVariant(risk) {
  if (risk === 'disruptive') return 'destructive'
  if (risk === 'configuration' || risk === 'service') return 'warn'
  if (risk === 'read-only') return 'muted'
  return 'default'
}

/** @param {string | null | undefined} risk */
function riskLabel(risk) {
  const labels = /** @type {Record<string, string>} */ ({
    'read-only': 'Read only',
    safe: 'Safe',
    'safe-mutation': 'Safe change',
    configuration: 'Configuration',
    service: 'Service action',
    disruptive: 'Disruptive',
  })
  return (risk && labels[risk]) || risk || 'Unknown'
}

/** @param {Control} control @param {boolean} online */
function isControlEnabled(control, online) {
  if (control.disabled === true) return false
  if (control.enabledWhen === 'online') return online === true
  if (control.enabledWhen === 'offline') return online === false
  return true
}

/** @param {Game} game @param {ServiceStatus | null | undefined} service */
function statusPresentation(game, service) {
  const states = /** @type {Record<string, { label: string, tone: string, variant: string }>} */ ({
    running_ready: { label: 'RUNNING', tone: 'good', variant: 'default' },
    running_degraded: { label: 'DEGRADED', tone: 'warn', variant: 'warn' },
    stopped: { label: 'STOPPED', tone: 'muted', variant: 'muted' },
    not_installed: { label: 'SETUP NEEDED', tone: 'warn', variant: 'warn' },
    unknown: { label: 'UNKNOWN', tone: 'muted', variant: 'muted' },
  })
  const state = service?.state && states[service.state]
    ? service.state
    : game.projectPresent === false || game.readiness === 'needs_setup'
      ? 'not_installed'
      : 'unknown'
  const reasons = []
  for (const blocker of game.blockers || []) {
    if (blocker.message) reasons.push(blocker.message)
  }
  if (service?.process?.error) reasons.push(service.process.error)
  for (const listener of service?.listeners || []) {
    if (listener.error) reasons.push(listener.error)
    else if (listener.ok === true && listener.listening === false && service?.process?.running === true) {
      reasons.push(`No ${String(listener.protocol || '').toUpperCase()} listener detected on port ${listener.port}.`)
    }
  }
  if (service?.query?.error) reasons.push(service.query.error)
  if (state === 'unknown' && reasons.length === 0) reasons.push('Status probes did not return a result.')
  return { state, ...states[state], reasons: [...new Set(reasons)] }
}

/** @param {ActionPlan} plan */
function confirmationCopy(plan) {
  const before = plan.currentValue === null || plan.currentValue === undefined
    ? 'current state'
    : JSON.stringify(plan.currentValue)
  const after = plan.proposedValue === null || plan.proposedValue === undefined
    ? plan.controlLabel
    : JSON.stringify(plan.proposedValue)
  const restart = plan.restartRequired
    ? ' A server restart is required before this takes effect.'
    : ''
  return `${plan.gameName}: ${plan.controlLabel}. Change ${before} to ${after}. Risk: ${plan.risk}.${restart}`
}

/** @param {ControlValue} value @param {string | undefined} unit */
function valueLabel(value, unit) {
  if (value === null || value === undefined) return 'Not available'
  return `${value}${unit ? ` ${unit}` : ''}`
}

/** @param {{ label: string, value: ControlValue }} props */
function Stat({ label, value }) {
  return jsx('div', {
    style: {
      ...panelStyle,
      backdropFilter: 'blur(6px)',
      backgroundColor: 'var(--ui-bg-editor)',
      background: 'color-mix(in srgb, var(--ui-bg-editor) 88%, transparent)',
      padding: '12px',
    },
    children: [
      jsx('div', { className: 'text-xs text-muted-foreground', children: label }, 'label'),
      jsx('div', { className: 'mt-1 truncate text-sm font-semibold', title: String(value), children: value }, 'value'),
    ],
  })
}

/**
 * @param {{
 *   control: Control,
 *   gameId: string,
 *   online: boolean,
 *   value: ControlValue,
 *   onValue: (value: ControlValue) => void,
 *   onPreview: (control: Control, value: ControlValue) => void,
 *   onRefresh: () => void,
 *   busy: boolean,
 * }} props
 */
function ControlCard({ control, gameId, online, value, onValue, onPreview, onRefresh, busy }) {
  const enabled = isControlEnabled(control, online) && !busy
  const action = control.binding?.action
  const body = []

  if (control.kind === 'button') {
    body.push(jsx(Button, {
      disabled: !enabled,
      onClick: () => action === 'ui.refresh' ? onRefresh() : onPreview(control, null),
      variant: control.variant || (control.risk === 'disruptive' ? 'destructive' : 'default'),
      children: busy ? 'Working…' : control.label,
    }, 'button'))
  } else if (control.kind === 'slider') {
    body.push(jsx('div', {
      className: 'flex items-center gap-3',
      children: [
        jsx('input', {
          'aria-label': control.label,
          disabled: !enabled,
          max: control.max,
          min: control.min,
          onChange: (/** @type {import('react').ChangeEvent<HTMLInputElement>} */ event) => onValue(Number(event.target.value)),
          step: control.step || 1,
          style: { accentColor: 'var(--ui-accent)', flex: 1 },
          type: 'range',
          value: value ?? control.min,
        }, 'range'),
        jsx('span', { className: 'min-w-16 text-right text-xs font-medium', children: valueLabel(value, control.unit) }, 'value'),
      ],
    }, 'slider'))
    body.push(jsx(Button, {
      disabled: !enabled,
      onClick: () => onPreview(control, value),
      size: 'sm',
      variant: 'secondary',
      children: 'Preview change',
    }, 'preview'))
  } else if (control.kind === 'switch') {
    body.push(jsx('div', {
      className: 'flex items-center justify-between gap-3',
      children: [
        jsx('span', { className: 'text-xs text-muted-foreground', children: value ? 'Enabled' : 'Disabled' }, 'label'),
        jsx(Switch, {
          'aria-label': control.label,
          checked: value === true,
          disabled: !enabled,
          onCheckedChange: (/** @type {boolean} */ checked) => onValue(Boolean(checked)),
        }, 'switch'),
      ],
    }, 'switch-row'))
    body.push(jsx(Button, {
      disabled: !enabled,
      onClick: () => onPreview(control, Boolean(value)),
      size: 'sm',
      variant: 'secondary',
      children: 'Preview change',
    }, 'preview'))
  } else if (control.kind === 'select') {
    const options = control.options || []
    body.push(jsx(Select, {
      disabled: !enabled,
      onValueChange: (/** @type {string} */ key) => onValue(selectValue(options, key)),
      value: value === null || value === undefined ? '' : selectKey(value),
      children: [
        jsx(SelectTrigger, {
          className: 'w-full',
          children: jsx(SelectValue, { placeholder: 'Choose a value' }),
        }, 'trigger'),
        jsx(SelectContent, {
          children: options.map(option => jsx(SelectItem, {
            value: selectKey(option.value),
            children: option.label,
          }, selectKey(option.value))),
        }, 'content'),
      ],
    }, 'select'))
    body.push(jsx(Button, {
      disabled: !enabled,
      onClick: () => onPreview(control, value),
      size: 'sm',
      variant: 'secondary',
      children: 'Preview change',
    }, 'preview'))
  } else if (control.kind === 'text' || control.kind === 'number') {
    const numberState = control.kind === 'number'
      ? numericDraft(value === null || value === undefined ? '' : String(value), control)
      : { valid: true }
    body.push(jsx(Input, {
      disabled: !enabled,
      max: control.max,
      maxLength: control.maxLength,
      min: control.min,
      onChange: (/** @type {import('react').ChangeEvent<HTMLInputElement>} */ event) => {
        if (control.kind !== 'number') onValue(event.target.value)
        else onValue(numericDraft(event.target.value, control).value)
      },
      step: control.step,
      type: control.kind === 'number' ? 'number' : 'text',
      value: value ?? '',
    }, 'input'))
    body.push(jsx(Button, {
      disabled: !enabled || !numberState.valid,
      onClick: () => onPreview(control, value),
      size: 'sm',
      variant: 'secondary',
      children: 'Preview change',
    }, 'preview'))
  } else {
    body.push(jsx('div', {
      className: 'text-lg font-semibold',
      children: valueLabel(value, control.unit),
    }, 'readonly'))
  }

  if (control.restartRequired) {
    body.push(jsx('p', {
      className: 'text-xs text-muted-foreground',
      children: 'Takes effect after a server restart.',
    }, 'restart'))
  }
  if (control.disabled && control.disabledReason) {
    body.push(jsx('p', {
      className: 'text-xs leading-5 text-amber-500',
      children: control.disabledReason,
    }, 'disabled-reason'))
  }

  return jsx('article', {
    'data-control': `${gameId}.${control.id}`,
    style: { ...panelStyle, padding: '14px' },
    children: [
      jsx('div', {
        className: 'mb-3 flex items-start justify-between gap-3',
        children: [
          jsx('div', {
            className: 'min-w-0',
            children: [
              jsx('h4', { className: 'text-sm font-semibold', children: control.label }, 'label'),
              control.help ? jsx('p', { className: 'mt-1 text-xs leading-5 text-muted-foreground', children: control.help }, 'help') : null,
            ],
          }, 'copy'),
          jsx(Badge, { variant: riskVariant(control.risk), children: riskLabel(control.risk) }, 'risk'),
        ],
      }, 'header'),
      jsx('div', { className: 'grid gap-3', children: body }, 'body'),
    ],
  })
}

/**
 * @param {{ tail: DiagnosticTail, onClose: () => void }} props
 */
function DiagnosticTailPanel({ tail, onClose }) {
  const loading = tail.state === 'loading'
  const stateLabel = loading ? 'Loading' : String(tail.state || 'unknown').replaceAll('_', ' ')
  const emptyMessage = tail.state === 'binary'
    ? 'This log is not readable text (binary content).'
    : tail.state === 'error'
      ? 'The redacted log tail could not be loaded.'
      : `No readable text (${tail.state || 'unknown'}).`

  return jsx('div', {
    'aria-live': 'polite',
    className: 'grid gap-3 p-3',
    style: { ...panelStyle, background: 'var(--ui-bg-secondary)' },
    children: [
      jsx('div', {
        className: 'flex items-center justify-between gap-3',
        children: [
          jsx('div', {
            className: 'min-w-0',
            children: [
              jsx('div', { className: 'truncate text-xs font-semibold', children: `Log tail · ${tail.logId || 'unknown'}` }, 'title'),
              jsx('div', { className: 'mt-1 text-xs text-muted-foreground', children: 'Redacted output from the selected approved log.' }, 'description'),
            ],
          }, 'copy'),
          jsx('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(Badge, { variant: loading ? 'muted' : tail.state === 'ok' ? 'default' : 'warn', children: stateLabel }, 'state'),
              jsx(Button, { disabled: loading, onClick: onClose, size: 'sm', variant: 'ghost', children: 'Close' }, 'close'),
            ],
          }, 'actions'),
        ],
      }, 'header'),
      loading
        ? jsx('div', { className: 'flex items-center gap-2 text-xs text-muted-foreground', children: [jsx(GlyphSpinner, {}, 'spinner'), ' Loading redacted tail…'] }, 'loading')
        : tail.content
          ? jsx('pre', {
              className: 'whitespace-pre-wrap break-words text-xs text-muted-foreground',
              style: { maxHeight: '280px', overflow: 'auto' },
              children: tail.content,
            }, 'content')
          : jsx('p', { className: 'text-xs text-muted-foreground', children: emptyMessage }, 'empty'),
    ],
  })
}

/**
 * @param {import('@hermes/plugin-sdk').PluginContext} ctx
 * @param {{ signal: AbortSignal }} runtime
 */
function createGameHostPage(ctx, runtime) {
  return function GameHostPage() {
    const t = usePluginI18n(ID)
    const viewport = useValue(host.state.viewport)
    const queryClient = useQueryClient()
    const [selectedGameId, setSelectedGameId] = useState(() => ctx.storage.get('selectedGame', 'minecraft'))
    const [drafts, setDrafts] = useState(/** @type {Record<string, ControlValue>} */ ({}))
    const [pendingPlan, setPendingPlan] = useState(/** @type {ActionPlan | null} */ (null))
    const [confirmPhrase, setConfirmPhrase] = useState('')
    const [activity, setActivity] = useState(/** @type {ActivityItem[]} */ ([]))
    const [showStore, setShowStore] = useState(false)
    const [restorePreview, setRestorePreview] = useState(/** @type {RestorePreview | null} */ (null))
    const [logTail, setLogTail] = useState(/** @type {DiagnosticTail | null} */ (null))
    const [restoreConfirmToken, setRestoreConfirmToken] = useState('')
    const [createBackupOpen, setCreateBackupOpen] = useState(false)
    const [restoreExecuteOpen, setRestoreExecuteOpen] = useState(false)

    const statusQuery = useQuery({
      queryKey: [ctx.source, 'status'],
      queryFn: () => /** @type {Promise<HostStatus>} */ (ctx.rest('/proxy/api/status')),
      refetchInterval: 10_000,
      retry: 1,
    })
    const catalogQuery = useQuery({
      queryKey: [ctx.source, 'catalog'],
      queryFn: () => /** @type {Promise<Catalog>} */ (ctx.rest('/proxy/api/controls')),
      refetchInterval: 30_000,
      retry: 1,
    })
    const storeQuery = useQuery({
      queryKey: [ctx.source, 'store'],
      queryFn: () => /** @type {Promise<{ store: Game[], installed: string[] }>} */ (
        ctx.rest('/proxy/api/store')
      ),
      refetchInterval: 30_000,
      retry: 1,
    })
    const artQuery = useQuery({
      queryKey: [ctx.source, 'art', selectedGameId],
      queryFn: () => /** @type {Promise<GameArt>} */ (ctx.rest(`/art/${selectedGameId}`)),
      enabled: !!selectedGameId,
      retry: false,
      staleTime: Infinity,
    })

    const backupsQuery = useQuery({
      queryKey: [ctx.source, 'backups', selectedGameId],
      queryFn: () => /** @type {Promise<{ gameId: string, backups: any[] }>} */ (
        ctx.rest(`/proxy/api/backups/${selectedGameId}`)
      ),
      refetchInterval: 60_000,
      retry: 1,
      enabled: !!selectedGameId,
    })

    const diagnosticsQuery = useQuery({
      queryKey: [ctx.source, 'diagnostics', selectedGameId],
      queryFn: () => /** @type {Promise<{ gameId: string, logs: Record<string, string> }>} */ (
        ctx.rest(`/proxy/api/diagnostics/${selectedGameId}/logs`)
      ),
      refetchInterval: 120_000,
      retry: 1,
      enabled: !!selectedGameId,
    })

    const createBackupMutation = useMutation({
      mutationFn: (/** @type {string} */ gameId) => /** @type {Promise<BackupArtifact>} */ (ctx.rest(`/proxy/api/backups/${gameId}/create`, {
        method: 'POST',
        body: { label: 'manual' },
        timeoutMs: 120_000,
      })),
    })

    const previewRestoreMutation = useMutation({
      mutationFn: (/** @type {RestorePreviewRequest} */ { gameId, artifactId, serverState }) => /** @type {Promise<RestorePreview>} */ (ctx.rest(`/proxy/api/backups/${gameId}/restore/preview`, {
        method: 'POST',
        body: { artifactId, serverState },
        timeoutMs: 30_000,
      })),
    })

    const executeRestoreMutation = useMutation({
      mutationFn: (/** @type {RestoreExecuteRequest} */ { gameId, previewId, confirmation, serverState }) => ctx.rest(`/proxy/api/backups/${gameId}/restore`, {
        method: 'POST',
        body: { previewId, confirmation, serverState },
        timeoutMs: 300_000,
      }),
    })

    const diagnosticsBundleMutation = useMutation({
      mutationFn: (/** @type {string} */ gameId) => /** @type {Promise<{ bundle: string }>} */ (ctx.rest(`/proxy/api/diagnostics/${gameId}/bundle`, {
        method: 'GET',
        timeoutMs: 30_000,
      })),
    })

    const planMutation = useMutation({
      mutationFn: (/** @type {PlanRequest} */ body) => /** @type {Promise<ActionPlan>} */ (ctx.rest('/proxy/api/control/plan', {
        method: 'POST',
        body: { ...body, actor: ACTOR },
        timeoutMs: 15_000,
      })),
    })
    const applyMutation = useMutation({
      mutationFn: async (/** @type {ActionPlan} */ plan) => {
        const queued = /** @type {OperationRecord} */ (await ctx.rest('/proxy/api/control/apply', {
          method: 'POST',
          body: { planId: plan.planId, planDigest: plan.planDigest, confirmed: true, actor: ACTOR },
          timeoutMs: 15_000,
        }))
        return waitForOperation((/** @type {string} */ path) => ctx.rest(path, { timeoutMs: 15_000 }), queued, { signal: runtime.signal })
      },
    })
    const installMutation = useMutation({
      mutationFn: (/** @type {{ gameId: string }} */ body) => ctx.rest('/proxy/api/store/install', {
        method: 'POST',
        body: { ...body, actor: ACTOR },
        timeoutMs: 30_000,
      }),
    })
    const uninstallMutation = useMutation({
      mutationFn: (/** @type {{ gameId: string }} */ body) => ctx.rest('/proxy/api/store/uninstall', {
        method: 'POST',
        body: { ...body, actor: ACTOR },
        timeoutMs: 30_000,
      }),
    })

    const catalog = catalogQuery.data
    const status = statusQuery.data
    const games = catalog?.games || []
    const installedGames = games.filter(item => item.installed === true)
    const storeItems = (storeQuery.data?.store) || []
    let game = games.find(item => item.id === selectedGameId && item.installed === true)
    if (!game) game = installedGames[0]
    const service = game ? status?.services?.[game.id] : null
    const online = service?.online === true
    const narrow = viewport?.narrow === true

    /** @param {string} gameId */
    function selectGame(gameId) {
      setSelectedGameId(gameId)
      setRestorePreview(null)
      setRestoreConfirmToken('')
      setRestoreExecuteOpen(false)
      setLogTail(null)
      ctx.storage.set('selectedGame', gameId)
    }

    /** @param {Control} control */
    function draftKey(control) {
      return `${game?.id || 'unknown'}:${control.id}`
    }

    /** @param {Control} control */
    function currentValue(control) {
      const key = draftKey(control)
      return Object.prototype.hasOwnProperty.call(drafts, key) ? drafts[key] : control.value
    }

    /** @param {Control} control @param {ControlValue} value */
    function setCurrentValue(control, value) {
      const key = draftKey(control)
      setDrafts(previous => ({ ...previous, [key]: value }))
    }

    async function refreshAll(showToast = false) {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: [ctx.source, 'status'] }),
        queryClient.invalidateQueries({ queryKey: [ctx.source, 'catalog'] }),
        queryClient.invalidateQueries({ queryKey: [ctx.source, 'store'] }),
        queryClient.invalidateQueries({ queryKey: [ctx.source, 'backups', selectedGameId] }),
        queryClient.invalidateQueries({ queryKey: [ctx.source, 'diagnostics', selectedGameId] }),
      ])
      setRestorePreview(null)
      setRestoreConfirmToken('')
      setRestoreExecuteOpen(false)
      setLogTail(null)
      if (showToast) host.notify({ kind: 'success', message: 'Game server state refreshed.' })
    }

    async function refreshStore() {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: [ctx.source, 'catalog'] }),
        queryClient.invalidateQueries({ queryKey: [ctx.source, 'store'] }),
        queryClient.invalidateQueries({ queryKey: [ctx.source, 'status'] }),
      ])
    }

    /** @param {string} gameId */
    async function handleInstall(gameId) {
      await installMutation.mutateAsync({ gameId })
      setShowStore(false)
      selectGame(gameId)
      await refreshStore()
      host.notify({ kind: 'success', message: 'Added to your console. Provision server files to start it.' })
    }

    /** @param {string} gameId */
    async function handleUninstall(gameId) {
      await uninstallMutation.mutateAsync({ gameId })
      if (selectedGameId === gameId) setSelectedGameId('')
      await refreshStore()
      host.notify({ kind: 'success', message: 'Removed from your console (server files kept).' })
    }

    /** @param {Control} control @param {ControlValue} value */
    async function preview(control, value) {
      if (!game) return
      try {
        const plan = await planMutation.mutateAsync({
          gameId: game.id,
          controlId: control.id,
          value,
        })
        setConfirmPhrase('')
        setPendingPlan(plan)
      } catch (error) {
        host.notifyError(error, 'Could not create an action preview')
      }
    }

    async function applyPending() {
      if (!pendingPlan) return
      if (pendingPlan.risk === 'disruptive' && confirmPhrase !== pendingPlan.gameName) {
        throw new Error(`Type ${pendingPlan.gameName} exactly to confirm this disruptive action.`)
      }
      const result = /** @type {OperationRecord} */ (await applyMutation.mutateAsync(pendingPlan))
      const key = `${pendingPlan.gameId}:${pendingPlan.controlId}`
      setDrafts(previous => {
        const next = { ...previous }
        delete next[key]
        return next
      })
      setActivity(previous => [{
        label: pendingPlan.controlLabel,
        detail: operationMessage(result),
        at: new Date().toLocaleTimeString(),
      }, ...previous].slice(0, 8))
      await refreshAll(false)
      if (!operationSucceeded(result)) {
        const error = new Error(`${pendingPlan.controlLabel} ${result.state}: ${operationMessage(result)}`)
        host.notifyError(error, 'Game server operation did not succeed')
        throw error
      }
      host.notify({ kind: 'success', message: `${pendingPlan.controlLabel} completed.` })
    }

    async function confirmCreateBackup() {
      if (!game) return
      setCreateBackupOpen(false)
      try {
        await createBackupMutation.mutateAsync(game.id)
        host.notify({ kind: 'success', message: `Backup created for ${game.name}.` })
        await queryClient.invalidateQueries({ queryKey: [ctx.source, 'backups', game.id] })
      } catch (error) {
        host.notifyError(error, 'Create backup failed')
      }
    }

    /** @param {{ artifactId?: string }} backup */
    async function handlePreviewRestoreFor(backup) {
      if (!game || !backup.artifactId) return
      if (online) {
        host.notify({ kind: 'info', message: `Stop ${game.name} before previewing a restore.` })
        return
      }
      try {
        const preview = await previewRestoreMutation.mutateAsync({
          gameId: game.id,
          artifactId: backup.artifactId,
          serverState: 'stopped',
        })
        setRestorePreview(preview)
        setRestoreConfirmToken('')
        setRestoreExecuteOpen(false)
        host.notify({ kind: 'info', message: `Restore preview ready for ${backup.artifactId}.` })
      } catch (error) {
        host.notifyError(error, 'Restore preview failed')
      }
    }

    async function confirmExecuteRestore() {
      if (!game || !restorePreview?.previewId) return
      if (online) {
        setRestoreExecuteOpen(false)
        host.notify({ kind: 'info', message: `Stop ${game.name} before restoring a backup.` })
        return
      }
      if (restoreConfirmToken !== restorePreview.requiredConfirmation) {
        host.notify({ kind: 'info', message: 'The confirmation phrase does not match the restore preview.' })
        return
      }
      try {
        await executeRestoreMutation.mutateAsync({
          gameId: game.id,
          previewId: restorePreview.previewId,
          confirmation: restoreConfirmToken,
          serverState: 'stopped',
        })
        setRestoreExecuteOpen(false)
        setRestorePreview(null)
        setRestoreConfirmToken('')
        host.notify({ kind: 'success', message: 'Restore completed and a safety backup was created.' })
        await queryClient.invalidateQueries({ queryKey: [ctx.source, 'backups', game.id] })
      } catch (error) {
        host.notifyError(error, 'Restore execute failed')
      }
    }

    /** @param {string} logId */
    async function handleLogTail(logId) {
      if (!game) return
      setLogTail({ logId, state: 'loading', content: '' })
      try {
        const tail = /** @type {DiagnosticTail} */ (await ctx.rest(`/proxy/api/diagnostics/${game.id}/logs/${encodeURIComponent(logId)}?redact=true`))
        setLogTail({ ...tail, logId: tail.logId || logId })
      } catch (error) {
        setLogTail({ logId, state: 'error', content: '' })
        host.notifyError(error, 'Log tail failed')
      }
    }

    async function handleBundle() {
      if (!game) return
      try {
        const result = await diagnosticsBundleMutation.mutateAsync(game.id)
        setLogTail({ state: 'ok', logId: 'diagnostics-bundle', content: result.bundle })
        host.notify({ kind: 'success', message: 'Redacted diagnostics bundle generated.' })
      } catch (error) {
        host.notifyError(error, 'Bundle generation failed')
      }
    }

    if (statusQuery.isLoading || catalogQuery.isLoading) {
      return jsx('div', {
        className: 'grid h-full content-start gap-3 p-6',
        children: [0, 1, 2, 3].map(item => jsx(Skeleton, { className: 'h-20 w-full' }, item)),
      })
    }

    const error = statusQuery.error || catalogQuery.error
    if (error) {
      return jsx('div', {
        className: 'grid h-full place-items-center p-6',
        children: jsx(ErrorState, {
          title: t('gameHost.unavailableTitle'),
          description: error instanceof Error ? error.message : t('gameHost.unavailableDescription'),
          children: jsx(Button, { onClick: () => refreshAll(false), children: t('gameHost.tryAgain') }),
        }),
      })
    }

    if (!game && !showStore) {
      return jsx('div', {
        className: 'grid h-full place-items-center p-6',
        children: jsx(ErrorState, {
          title: 'No servers installed',
          description: 'Add a supported game from the Store to manage it here.',
          children: jsx(Button, { onClick: () => setShowStore(true), children: 'Browse the Store' }),
        }),
      })
    }

    const presentation = game ? statusPresentation(game, service) : null
    const process = service?.process || {}
    const connect = service?.connect || {}
    const groups = /** @type {Map<string, Control[]>} */ (new Map())
    for (const control of (game?.controls || [])) {
      const name = control.group || 'Controls'
      const controls = groups.get(name)
      if (controls) controls.push(control)
      else groups.set(name, [control])
    }

    const navigation = jsx('aside', {
      style: {
        borderBottom: narrow ? '1px solid var(--ui-stroke-secondary)' : undefined,
        borderRight: narrow ? undefined : '1px solid var(--ui-stroke-secondary)',
        minWidth: 0,
        overflow: 'auto',
        padding: '16px',
      },
      children: [
        jsx('div', {
          className: 'mb-4',
          children: [
            jsx('div', { className: 'text-sm font-semibold', children: 'Hosted games' }, 'title'),
            jsx('div', { className: 'mt-1 text-xs text-muted-foreground', children: 'Only installed servers · add via Store' }, 'subtitle'),
          ],
        }, 'heading'),
        jsx('div', {
          className: narrow ? 'flex gap-2 overflow-x-auto' : 'grid gap-2',
          children: installedGames.length === 0
            ? jsx('p', { className: 'text-xs text-muted-foreground', children: 'No installed servers yet. Open the Store to add one.' }, 'empty')
            : installedGames.map(item => {
                const itemStatus = statusPresentation(item, status?.services?.[item.id])
                return jsx(Button, {
                  className: narrow ? 'shrink-0 justify-start' : 'w-full justify-start',
                  onClick: () => selectGame(item.id),
                  variant: item.id === game?.id ? 'secondary' : 'ghost',
                  children: [
                    jsx(StatusDot, { tone: itemStatus.tone }, 'dot'),
                    jsx('span', { children: item.name }, 'name'),
                  ],
                }, item.id)
              }),
        }, 'games'),
        jsx(Button, {
          className: narrow ? 'shrink-0 justify-start' : 'w-full justify-start',
          onClick: () => setShowStore(value => !value),
          variant: showStore ? 'secondary' : 'ghost',
          children: showStore ? 'Back to console' : 'Browse the Store',
        }, 'store-toggle'),
        narrow ? null : jsx('div', {
          className: 'mt-6 grid gap-2 text-xs text-muted-foreground',
          children: [
            jsx(Separator, {}, 'sep'),
            jsx('p', { children: 'Profiles cannot provide shell commands. All mutations use backend-owned adapters.' }, 'copy'),
          ],
        }, 'safety'),
      ],
    })

    if (showStore) {
      const installedSection = jsx('section', {
        style: { ...panelStyle, padding: '16px', marginBottom: '20px' },
        children: [
          jsx('h2', { className: 'text-sm font-semibold', children: 'Installed' }, 'title'),
          installedGames.length === 0
            ? jsx('p', { className: 'mt-2 text-xs text-muted-foreground', children: 'Nothing installed yet. Add a game from the list below.' }, 'empty')
            : jsx('div', {
                className: 'mt-3 grid gap-2',
                children: installedGames.map(item => jsx('div', {
                  key: item.id,
                  className: 'flex items-center justify-between gap-3',
                  children: [
                    jsx('span', { className: 'text-sm font-medium', children: item.name }, 'name'),
                    jsx(Button, {
                      disabled: uninstallMutation.isPending,
                      onClick: () => handleUninstall(item.id),
                      size: 'sm',
                      variant: 'ghost',
                      children: 'Remove',
                    }, 'remove'),
                  ],
                }, item.id)),
              }, 'list'),
        ],
      }, 'installed')
      const availableSection = jsx('section', {
        style: { ...panelStyle, padding: '16px' },
        children: [
          jsx('h2', { className: 'text-sm font-semibold', children: 'Available to add' }, 'title'),
          jsx('p', { className: 'mt-1 text-xs leading-5 text-muted-foreground', children: 'Adding a game creates its project home here. Provision the server files afterwards with the Hermes game-host skill or manual setup.' }, 'sub'),
          storeItems.length === 0
            ? jsx('p', { className: 'mt-3 text-sm text-muted-foreground', children: 'Every supported game is already installed.' }, 'empty')
            : jsx('div', {
                className: 'mt-3 grid gap-3',
                children: storeItems.map(item => jsx('div', {
                  key: item.id,
                  style: { ...panelStyle, padding: '14px' },
                  children: [
                    jsx('div', {
                      className: 'flex items-start justify-between gap-3',
                      children: [
                        jsx('div', {
                          className: 'min-w-0',
                          children: [
                            jsx('h4', { className: 'text-sm font-semibold', children: item.name }, 'name'),
                            item.description ? jsx('p', { className: 'mt-1 text-xs leading-5 text-muted-foreground', children: item.description }, 'desc') : null,
                          ],
                        }, 'copy'),
                        jsx(Button, {
                          disabled: installMutation.isPending,
                          onClick: () => handleInstall(item.id),
                          size: 'sm',
                          variant: 'secondary',
                          children: installMutation.isPending ? 'Adding…' : 'Add',
                        }, 'install'),
                      ],
                    }, 'row'),
                  ],
                }, item.id)),
              }, 'list'),
        ],
      }, 'available')
      return jsx('div', {
        className: 'h-full min-h-0 w-full overflow-hidden',
        style: {
          display: 'grid',
          gridTemplateColumns: narrow ? 'minmax(0, 1fr)' : '240px minmax(0, 1fr)',
          gridTemplateRows: narrow ? 'auto minmax(0, 1fr)' : 'minmax(0, 1fr)',
        },
        children: [
          navigation,
          jsx(ScrollArea, {
            className: 'h-full min-h-0',
            children: jsx('main', {
              style: { margin: '0 auto', maxWidth: '980px', padding: narrow ? '16px' : '24px' },
              children: [installedSection, availableSection],
            }),
          }),
        ],
      })
    }

    if (!game || !presentation) {
      return jsx('div', {
        className: 'grid h-full place-items-center p-6',
        children: jsx(ErrorState, {
          title: 'Server selection unavailable',
          description: 'Refresh the catalog or choose an installed server.',
          children: jsx(Button, { onClick: () => refreshAll(false), children: 'Refresh' }),
        }),
      })
    }

    const content = jsx(ScrollArea, {
      className: 'h-full min-h-0',
      children: jsx('main', {
        style: { margin: '0 auto', maxWidth: '1180px', padding: narrow ? '16px' : '24px' },
        children: [
          jsx('section', {
            style: {
              ...panelStyle,
              overflow: 'hidden',
              padding: narrow ? '16px' : '20px',
              position: 'relative',
            },
            children: [
              artQuery.data?.dataUrl ? jsx('img', {
                alt: '',
                'aria-hidden': true,
                loading: 'eager',
                src: artQuery.data.dataUrl,
                style: {
                  height: '100%',
                  inset: 0,
                  objectFit: 'cover',
                  objectPosition: artQuery.data?.objectPosition || '50% 50%',
                  opacity: narrow ? 0.4 : 0.78,
                  pointerEvents: 'none',
                  position: 'absolute',
                  width: '100%',
                },
              }, 'art') : null,
              artQuery.data?.dataUrl ? jsx('div', {
                'aria-hidden': true,
                style: {
                  backgroundColor: 'var(--ui-bg-editor)',
                  background: 'linear-gradient(90deg, color-mix(in srgb, var(--ui-bg-editor) 96%, transparent) 0%, color-mix(in srgb, var(--ui-bg-editor) 82%, transparent) 48%, color-mix(in srgb, var(--ui-bg-editor) 34%, transparent) 100%)',
                  inset: 0,
                  pointerEvents: 'none',
                  position: 'absolute',
                },
              }, 'scrim') : null,
              jsx('div', {
                className: 'flex flex-wrap items-start justify-between gap-4',
                style: { position: 'relative', zIndex: 1 },
                children: [
                  jsx('div', {
                    style: { flex: '1 1 560px', minWidth: 0 },
                    children: [
                      jsx('div', {
                        className: 'flex items-center gap-2',
                        children: [
                          jsx(StatusDot, { tone: presentation.tone }, 'dot'),
                          jsx(Badge, { variant: presentation.variant, children: presentation.label }, 'badge'),
                        ],
                      }, 'status'),
                      jsx('h1', { className: 'mt-3 text-2xl font-semibold tracking-tight', children: game.name }, 'name'),
                      jsx('p', { className: 'mt-2 max-w-2xl text-sm leading-6 text-muted-foreground', children: game.description || 'Game-specific controls managed by Hermes.' }, 'description'),
                      presentation.reasons.length ? jsx('div', {
                        className: 'mt-3 grid gap-1 rounded-md border border-amber-500/30 p-3 text-xs leading-5 text-amber-500',
                        style: {
                          backdropFilter: 'blur(6px)',
                          backgroundColor: 'var(--ui-bg-editor)',
                          background: 'color-mix(in srgb, var(--ui-bg-editor) 94%, transparent)',
                        },
                        children: presentation.reasons.map((reason, index) => jsx('p', { children: reason }, `${index}-${reason}`)),
                      }, 'status-reasons') : null,
                      game.hints && game.hints.length ? jsx('div', {
                        className: 'mt-3 grid gap-1 rounded-md border border-neutral-500/20 p-3 text-xs leading-5 text-muted-foreground',
                        style: {
                          backdropFilter: 'blur(6px)',
                          backgroundColor: 'var(--ui-bg-editor)',
                          background: 'color-mix(in srgb, var(--ui-bg-editor) 94%, transparent)',
                        },
                        children: [
                          jsx('p', { className: 'font-medium', children: 'Setup hints' }, 'title'),
                          ...game.hints.map((hint, index) => jsx('p', { children: hint.message }, `hint-${index}`)),
                        ],
                      }, 'setup-hints') : null,
                    ],
                  }, 'copy'),
                  jsx(Button, {
                    disabled: statusQuery.isFetching || catalogQuery.isFetching,
                    onClick: () => refreshAll(true),
                    style: { backgroundColor: 'var(--ui-bg-editor)', flexShrink: 0 },
                    variant: 'secondary',
                    children: statusQuery.isFetching || catalogQuery.isFetching
                      ? [jsx(GlyphSpinner, {}, 'spin'), ' Refreshing']
                      : 'Refresh',
                  }, 'refresh'),
                ],
              }, 'header'),
              jsx('div', {
                className: 'mt-5 grid gap-3',
                style: {
                  gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))',
                  position: 'relative',
                  zIndex: 1,
                },
                children: [
                  jsx(Stat, { label: 'Connect', value: connect.lan || connect.local || connect.public || 'Unavailable' }, 'connect'),
                  jsx(Stat, { label: 'Uptime', value: process.uptimeHuman || '—' }, 'uptime'),
                  jsx(Stat, { label: 'Players', value: service?.players ? `${service.players.online ?? 0} / ${service.players.max ?? 0}` : '—' }, 'players'),
                  jsx(Stat, { label: 'Memory', value: process.rssMB ? `${process.rssMB} MB` : '—' }, 'memory'),
                ],
              }, 'stats'),
            ],
          }, 'hero'),
          ...Array.from(groups.entries()).map(([name, controls]) => jsx('section', {
            className: 'mt-6',
            children: [
              jsx('div', {
                className: 'mb-3 flex items-end justify-between gap-3',
                children: [
                  jsx('h2', { className: 'text-sm font-semibold', children: name }, 'name'),
                  jsx('span', { className: 'text-xs text-muted-foreground', children: `${controls.length} control${controls.length === 1 ? '' : 's'}` }, 'count'),
                ],
              }, 'heading'),
              jsx('div', {
                className: 'grid gap-3',
                style: { gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))' },
                children: controls.map(control => jsx(ControlCard, {
                  busy: planMutation.isPending || applyMutation.isPending,
                  control,
                  gameId: game.id,
                  online,
                  onPreview: preview,
                  onRefresh: () => refreshAll(true),
                  onValue: (/** @type {ControlValue} */ value) => setCurrentValue(control, value),
                  value: currentValue(control),
                }, control.id)),
              }, 'controls'),
            ],
          }, name)),

                    // === Backups (simplified but wired) ===
          jsx('section', {
            className: 'mt-6',
            style: { ...panelStyle, padding: '16px' },
            children: [
              jsx('div', {
                className: 'flex items-center justify-between mb-2',
                children: [
                  jsx('h2', { className: 'text-sm font-semibold', children: 'Backups' }),
                  jsx(Button, {
                    disabled: createBackupMutation.isPending || !game,
                    onClick: () => setCreateBackupOpen(true),
                    size: 'sm',
                    variant: 'secondary',
                    children: createBackupMutation.isPending ? 'Creating…' : 'Create backup',
                  }, 'create'),
                ],
              }),
              backupsQuery.isError
                ? jsx('p', { className: 'mt-2 text-xs text-muted-foreground', children: backupsQuery.error instanceof Error ? backupsQuery.error.message : 'Backups are unavailable for this game.' }, 'error')
                : backupsQuery.isLoading
                  ? jsx('p', { className: 'mt-2 text-xs text-muted-foreground', children: 'Loading backups…' }, 'loading')
                  : (!backupsQuery.data?.backups || backupsQuery.data.backups.length === 0)
                    ? jsx('p', { className: 'mt-2 text-xs text-muted-foreground', children: 'No backups for this game yet.' }, 'empty')
                    : jsx('div', {
                  className: 'mt-3 grid gap-2',
                  children: backupsQuery.data.backups.map((/** @type {any} */ backup) => {
                    const sizeMB = Number.isFinite(backup.sizeBytes)
                      ? `${Math.max(0.01, backup.sizeBytes / 1024 / 1024).toFixed(2)} MB`
                      : 'Unknown size'
                    const validation = backup.validation?.state || 'unknown'
                    const entries = backup.validation?.entryCount
                    return jsx('div', {
                      className: 'flex items-center justify-between gap-3 p-3',
                      style: panelStyle,
                      children: [
                        jsx('div', {
                          className: 'min-w-0',
                          children: [
                            jsx('div', { className: 'truncate text-xs font-medium', title: backup.artifactId, children: backup.filename || backup.artifactId }, 'name'),
                            jsx('div', {
                              className: 'mt-1 text-xs text-muted-foreground',
                              children: `${sizeMB} · ${validation}${Number.isFinite(entries) ? ` · ${entries} entries` : ''}`,
                            }, 'meta'),
                          ],
                        }, 'copy'),
                        jsx(Button, {
                          disabled: online || previewRestoreMutation.isPending,
                          onClick: () => handlePreviewRestoreFor(backup),
                          size: 'sm',
                          variant: 'ghost',
                          children: online ? 'Stop to restore' : 'Preview restore',
                        }, 'preview'),
                      ],
                    }, backup.artifactId)
                  }),
                }),
              restorePreview ? jsx('div', {
                className: 'mt-3 grid gap-3 p-3',
                style: panelStyle,
                children: [
                  jsx('div', {
                    children: [
                      jsx('div', { className: 'text-xs font-medium', children: `Restore preview: ${restorePreview.artifactId}` }, 'title'),
                      jsx('div', { className: 'mt-1 text-xs text-muted-foreground', children: `${restorePreview.archiveEntries?.length || 0} archive entries will be restored. The server must remain stopped.` }, 'summary'),
                    ],
                  }, 'copy'),
                  jsx('code', { className: 'break-all text-xs text-muted-foreground', children: restorePreview.requiredConfirmation }, 'required'),
                  jsx('div', {
                    className: 'flex gap-2',
                    children: [
                      jsx(Input, {
                        autoComplete: 'off',
                        className: 'flex-1 text-xs',
                        onChange: (/** @type {import('react').ChangeEvent<HTMLInputElement>} */ event) => setRestoreConfirmToken(event.target.value),
                        placeholder: 'Type the exact confirmation phrase',
                        value: restoreConfirmToken,
                      }, 'token'),
                      jsx(Button, {
                        disabled: executeRestoreMutation.isPending || restoreConfirmToken !== restorePreview.requiredConfirmation,
                        onClick: () => setRestoreExecuteOpen(true),
                        size: 'sm',
                        variant: 'destructive',
                        children: executeRestoreMutation.isPending ? 'Restoring…' : 'Review restore',
                      }, 'execute'),
                    ],
                  }, 'actions'),
                ],
              }, 'restore-preview') : null,
            ],
          }, 'backups'),

          jsx('section', {
            className: 'mt-6 mb-8',
            style: { ...panelStyle, padding: '16px' },
            children: [
              jsx('div', {
                className: 'flex items-center justify-between gap-3',
                children: [
                  jsx('div', {
                    children: [
                      jsx('h2', { className: 'text-sm font-semibold', children: 'Diagnostics & logs' }, 'title'),
                      jsx('p', { className: 'mt-1 text-xs text-muted-foreground', children: 'Bounded, redacted output from approved server logs.' }, 'description'),
                    ],
                  }, 'copy'),
                  jsx(Button, {
                    disabled: diagnosticsBundleMutation.isPending,
                    onClick: handleBundle,
                    size: 'sm',
                    variant: 'secondary',
                    children: diagnosticsBundleMutation.isPending ? 'Generating…' : 'Generate bundle',
                  }, 'bundle'),
                ],
              }, 'header'),
              logTail?.logId === 'diagnostics-bundle'
                ? jsx(DiagnosticTailPanel, { tail: logTail, onClose: () => setLogTail(null) }, 'bundle-output')
                : null,
              diagnosticsQuery.isError
                ? jsx('p', { className: 'mt-3 text-xs text-muted-foreground', children: diagnosticsQuery.error instanceof Error ? diagnosticsQuery.error.message : 'Diagnostics are unavailable for this game.' }, 'error')
                : diagnosticsQuery.isLoading
                  ? jsx('p', { className: 'mt-3 text-xs text-muted-foreground', children: 'Loading logs…' }, 'loading')
                  : (!diagnosticsQuery.data?.logs || Object.keys(diagnosticsQuery.data.logs).length === 0)
                    ? jsx('p', { className: 'mt-3 text-xs text-muted-foreground', children: 'No approved logs are available.' }, 'empty')
                    : jsx('div', {
                        className: 'mt-3 grid gap-2',
                        children: Object.entries(diagnosticsQuery.data.logs).map(([logId, relativePath]) => jsx('div', {
                          className: 'grid gap-2',
                          children: [
                            jsx('div', {
                              className: 'flex items-center justify-between gap-3 p-3',
                              style: panelStyle,
                              children: [
                                jsx('div', {
                                  className: 'min-w-0',
                                  children: [
                                    jsx('div', { className: 'text-xs font-medium', children: logId }, 'id'),
                                    jsx('div', { className: 'mt-1 truncate text-xs text-muted-foreground', title: String(relativePath), children: relativePath }, 'path'),
                                  ],
                                }, 'copy'),
                                jsx(Button, {
                                  disabled: logTail?.state === 'loading',
                                  onClick: () => handleLogTail(logId),
                                  size: 'sm',
                                  variant: 'ghost',
                                  children: logTail?.logId === logId && logTail.state === 'loading'
                                    ? 'Loading…'
                                    : logTail?.logId === logId
                                      ? 'Refresh tail'
                                      : 'View tail',
                                }, 'view'),
                              ],
                            }, 'row'),
                            logTail?.logId === logId
                              ? jsx(DiagnosticTailPanel, { tail: logTail, onClose: () => setLogTail(null) }, 'tail')
                              : null,
                          ],
                        }, logId)),
                      }, 'logs'),
            ],
          }, 'diagnostics'),

          jsx('section', {
            className: 'mt-6 mb-8',
            style: { ...panelStyle, padding: '16px' },
            children: [
              jsx('h2', { className: 'text-sm font-semibold', children: 'This session' }, 'title'),
              activity.length === 0
                ? jsx('p', { className: 'mt-2 text-xs text-muted-foreground', children: 'No actions have been applied from this Desktop session.' }, 'empty')
                : jsx('div', {
                    className: 'mt-3 grid gap-3',
                    children: activity.map((item, index) => jsx('div', {
                      className: 'flex items-start justify-between gap-4 text-xs',
                      children: [
                        jsx('div', {
                          children: [
                            jsx('div', { className: 'font-medium', children: item.label }, 'label'),
                            jsx('div', { className: 'mt-1 whitespace-pre-wrap text-muted-foreground', children: item.detail }, 'detail'),
                          ],
                        }, 'copy'),
                        jsx('time', { className: 'shrink-0 text-muted-foreground', children: item.at }, 'time'),
                      ],
                    }, `${item.at}-${index}`)),
                  }, 'items'),
            ],
          }, 'activity'),
          jsx(ConfirmDialog, {
            cancelLabel: 'Cancel',
            confirmLabel: 'Create backup',
            onClose: () => setCreateBackupOpen(false),
            onConfirm: confirmCreateBackup,
            open: createBackupOpen,
            title: `Create backup for ${game.name}?`,
            description: 'The approved save data will be archived and verified before it appears in the inventory.',
          }, 'backup-confirm'),
          jsx(ConfirmDialog, {
            cancelLabel: 'Cancel',
            confirmLabel: 'Restore backup',
            destructive: true,
            onClose: () => setRestoreExecuteOpen(false),
            onConfirm: confirmExecuteRestore,
            open: restoreExecuteOpen,
            title: 'Final restore confirmation',
            description: restorePreview
              ? `Restore ${restorePreview.artifactId}? Live save data will be replaced after a verified safety backup is created.`
              : '',
          }, 'restore-confirm'),
          jsx(ConfirmDialog, {
            cancelLabel: 'Cancel',
            confirmLabel: pendingPlan?.risk === 'disruptive' ? 'Confirm and run' : 'Apply change',
            destructive: pendingPlan?.risk === 'disruptive',
            onClose: () => {
              setPendingPlan(null)
              setConfirmPhrase('')
            },
            onConfirm: applyPending,
            open: Boolean(pendingPlan),
            title: pendingPlan?.risk === 'disruptive' ? 'Confirm disruptive action' : 'Review proposed action',
            description: pendingPlan ? jsx('div', {
              className: 'grid gap-3',
              children: [
                jsx('p', { children: confirmationCopy(pendingPlan) }, 'copy'),
                pendingPlan.risk === 'disruptive'
                  ? jsx('div', {
                      className: 'grid gap-2',
                      children: [
                        jsx('label', { className: 'text-xs font-medium', children: `Type ${pendingPlan.gameName} to continue` }, 'label'),
                        jsx(Input, {
                          autoComplete: 'off',
                          onChange: (/** @type {import('react').ChangeEvent<HTMLInputElement>} */ event) => setConfirmPhrase(event.target.value),
                          value: confirmPhrase,
                        }, 'input'),
                      ],
                    }, 'phrase')
                  : null,
              ],
            }) : null,
          }, 'confirm'),
        ],
      }),
    })

    return jsx('div', {
      className: 'h-full min-h-0 w-full overflow-hidden',
      style: {
        display: 'grid',
        gridTemplateColumns: narrow ? 'minmax(0, 1fr)' : '240px minmax(0, 1fr)',
        gridTemplateRows: narrow ? 'auto minmax(0, 1fr)' : 'minmax(0, 1fr)',
      },
      children: [navigation, content],
    })
  }
}

const plugin = /** @type {import('@hermes/plugin-sdk').HermesPlugin} */ ({
  id: ID,
  name: 'Game Host Console',
  defaultEnabled: false,
  register(ctx) {
    const controller = new AbortController()
    ctx.onDispose(() => controller.abort())
    const runtime = { signal: controller.signal }
    ctx.i18n.register({
      en: {
        gameHost: {
          navLabel: 'Game Host',
          openLabel: 'Open Game Host Console',
          unavailableTitle: 'Game Host Console unavailable',
          unavailableDescription: 'The authenticated backend bridge could not be reached.',
          tryAgain: 'Try again',
        },
      },
    })
    const GameHostPage = createGameHostPage(ctx, runtime)
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => jsx(GameHostPage, {}),
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 65,
        data: { path: ROUTE, label: ctx.i18n.t('gameHost.navLabel'), codicon: 'server-process' },
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'game-host-console.open',
          label: ctx.i18n.t('gameHost.openLabel'),
          keywords: ['game', 'server', 'minecraft', 'palworld', 'host'],
          run: () => host.navigate(ROUTE),
        },
      },
    ])
  },
})

export default plugin