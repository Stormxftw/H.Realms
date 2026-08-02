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
import {
  numericDraft,
  operationMessage,
  operationSucceeded,
  selectKey,
  selectValue,
  waitForOperation,
} from './behavior.mjs'

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
 * @property {object[]} [blockers]
 * @property {object[]} [hints]
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
    style: { ...panelStyle, padding: '12px' },
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
        return waitForOperation(path => ctx.rest(path, { timeoutMs: 15_000 }), queued, { signal: runtime.signal })
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
      ])
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

    const content = jsx(ScrollArea, {
      className: 'h-full min-h-0',
      children: jsx('main', {
        style: { margin: '0 auto', maxWidth: '1180px', padding: narrow ? '16px' : '24px' },
        children: [
          jsx('section', {
            style: { ...panelStyle, padding: narrow ? '16px' : '20px' },
            children: [
              jsx('div', {
                className: 'flex flex-wrap items-start justify-between gap-4',
                children: [
                  jsx('div', {
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
                        className: 'mt-3 grid gap-1 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-xs leading-5 text-amber-500',
                        children: presentation.reasons.map((reason, index) => jsx('p', { children: reason }, `${index}-${reason}`)),
                      }, 'status-reasons') : null,
                      game.hints && game.hints.length ? jsx('div', {
                        className: 'mt-3 grid gap-1 rounded-md border border-neutral-500/20 bg-neutral-500/5 p-3 text-xs leading-5 text-muted-foreground',
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
                    variant: 'secondary',
                    children: statusQuery.isFetching || catalogQuery.isFetching
                      ? [jsx(GlyphSpinner, {}, 'spin'), ' Refreshing']
                      : 'Refresh',
                  }, 'refresh'),
                ],
              }, 'header'),
              jsx('div', {
                className: 'mt-5 grid gap-3',
                style: { gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))' },
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