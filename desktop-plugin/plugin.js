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
  useQuery,
  useQueryClient,
  useValue,
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx } from 'react/jsx-runtime'

const ID = 'game-host-console'
const ROUTE = '/game-host'
const ACTOR = 'hermes-desktop'

const panelStyle = {
  border: '1px solid var(--ui-stroke-secondary)',
  borderRadius: '8px',
}

function riskVariant(risk) {
  if (risk === 'disruptive') return 'destructive'
  if (risk === 'configuration' || risk === 'service') return 'warn'
  if (risk === 'read-only') return 'muted'
  return 'default'
}

function riskLabel(risk) {
  return {
    'read-only': 'Read only',
    safe: 'Safe',
    'safe-mutation': 'Safe change',
    configuration: 'Configuration',
    service: 'Service action',
    disruptive: 'Disruptive',
  }[risk] || risk || 'Unknown'
}

function isControlEnabled(control, online) {
  if (control.disabled === true) return false
  if (control.enabledWhen === 'online') return online === true
  if (control.enabledWhen === 'offline') return online === false
  return true
}

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

function valueLabel(value, unit) {
  if (value === null || value === undefined) return 'Not available'
  return `${value}${unit ? ` ${unit}` : ''}`
}

function Stat({ label, value }) {
  return jsx('div', {
    style: { ...panelStyle, padding: '12px' },
    children: [
      jsx('div', { className: 'text-xs text-muted-foreground', children: label }, 'label'),
      jsx('div', { className: 'mt-1 truncate text-sm font-semibold', title: String(value), children: value }, 'value'),
    ],
  })
}

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
          onChange: event => onValue(Number(event.target.value)),
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
          onCheckedChange: checked => onValue(Boolean(checked)),
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
    body.push(jsx(Select, {
      disabled: !enabled,
      onValueChange: onValue,
      value: value === null || value === undefined ? '' : String(value),
      children: [
        jsx(SelectTrigger, {
          className: 'w-full',
          children: jsx(SelectValue, { placeholder: 'Choose a value' }),
        }, 'trigger'),
        jsx(SelectContent, {
          children: (control.options || []).map(option => jsx(SelectItem, {
            value: String(option.value),
            children: option.label,
          }, String(option.value))),
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
    body.push(jsx(Input, {
      disabled: !enabled,
      max: control.max,
      maxLength: control.maxLength,
      min: control.min,
      onChange: event => onValue(control.kind === 'number' ? Number(event.target.value) : event.target.value),
      step: control.step,
      type: control.kind === 'number' ? 'number' : 'text',
      value: value ?? '',
    }, 'input'))
    body.push(jsx(Button, {
      disabled: !enabled,
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

function createGameHostPage(ctx) {
  return function GameHostPage() {
    const viewport = useValue(host.state.viewport)
    const queryClient = useQueryClient()
    const [selectedGameId, setSelectedGameId] = useState(() => ctx.storage.get('selectedGame', 'minecraft'))
    const [drafts, setDrafts] = useState({})
    const [pendingPlan, setPendingPlan] = useState(null)
    const [confirmPhrase, setConfirmPhrase] = useState('')
    const [activity, setActivity] = useState([])

    const statusQuery = useQuery({
      queryKey: [ctx.source, 'status'],
      queryFn: () => ctx.rest('/proxy/api/status'),
      refetchInterval: 10_000,
      retry: 1,
    })
    const catalogQuery = useQuery({
      queryKey: [ctx.source, 'catalog'],
      queryFn: () => ctx.rest('/proxy/api/controls'),
      refetchInterval: 30_000,
      retry: 1,
    })

    const planMutation = useMutation({
      mutationFn: body => ctx.rest('/proxy/api/control/plan', {
        method: 'POST',
        body: { ...body, actor: ACTOR },
        timeoutMs: 15_000,
      }),
    })
    const applyMutation = useMutation({
      mutationFn: plan => ctx.rest('/proxy/api/control/apply', {
        method: 'POST',
        body: { planId: plan.planId, planDigest: plan.planDigest, confirmed: true, actor: ACTOR },
        timeoutMs: 320_000,
      }),
    })

    const catalog = catalogQuery.data
    const status = statusQuery.data
    const games = catalog?.games || []
    const game = games.find(item => item.id === selectedGameId) || games[0]
    const service = game ? status?.services?.[game.id] : null
    const online = service?.online === true
    const narrow = viewport?.narrow === true

    function selectGame(gameId) {
      setSelectedGameId(gameId)
      ctx.storage.set('selectedGame', gameId)
    }

    function draftKey(control) {
      return `${game?.id || 'unknown'}:${control.id}`
    }

    function currentValue(control) {
      const key = draftKey(control)
      return Object.prototype.hasOwnProperty.call(drafts, key) ? drafts[key] : control.value
    }

    function setCurrentValue(control, value) {
      const key = draftKey(control)
      setDrafts(previous => ({ ...previous, [key]: value }))
    }

    async function refreshAll(showToast = false) {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: [ctx.source, 'status'] }),
        queryClient.invalidateQueries({ queryKey: [ctx.source, 'catalog'] }),
      ])
      if (showToast) host.notify({ kind: 'success', message: 'Game server state refreshed.' })
    }

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
      const result = await applyMutation.mutateAsync(pendingPlan)
      const key = `${pendingPlan.gameId}:${pendingPlan.controlId}`
      setDrafts(previous => {
        const next = { ...previous }
        delete next[key]
        return next
      })
      setActivity(previous => [{
        label: pendingPlan.controlLabel,
        detail: result.output || 'Completed successfully.',
        at: new Date().toLocaleTimeString(),
      }, ...previous].slice(0, 8))
      await refreshAll(false)
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
          title: 'Game Host Console unavailable',
          description: error instanceof Error ? error.message : 'The authenticated backend bridge could not be reached.',
          children: jsx(Button, { onClick: () => refreshAll(false), children: 'Try again' }),
        }),
      })
    }

    if (!game) {
      return jsx('div', {
        className: 'grid h-full place-items-center p-6',
        children: jsx(ErrorState, {
          title: 'No game profiles installed',
          description: 'Add a validated declarative profile before using the console.',
        }),
      })
    }

    const process = service?.process || {}
    const connect = service?.connect || {}
    const groups = new Map()
    for (const control of game.controls || []) {
      const name = control.group || 'Controls'
      if (!groups.has(name)) groups.set(name, [])
      groups.get(name).push(control)
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
            jsx('div', { className: 'mt-1 text-xs text-muted-foreground', children: 'Typed controls · preview before apply' }, 'subtitle'),
          ],
        }, 'heading'),
        jsx('div', {
          className: narrow ? 'flex gap-2 overflow-x-auto' : 'grid gap-2',
          children: games.map(item => {
            const itemOnline = status?.services?.[item.id]?.online === true
            return jsx(Button, {
              className: narrow ? 'shrink-0 justify-start' : 'w-full justify-start',
              onClick: () => selectGame(item.id),
              variant: item.id === game.id ? 'secondary' : 'ghost',
              children: [
                jsx(StatusDot, { tone: itemOnline ? 'good' : 'muted' }, 'dot'),
                jsx('span', { children: item.name }, 'name'),
              ],
            }, item.id)
          }),
        }, 'games'),
        narrow ? null : jsx('div', {
          className: 'mt-6 grid gap-2 text-xs text-muted-foreground',
          children: [
            jsx(Separator, {}, 'sep'),
            jsx('p', { children: 'Profiles cannot provide shell commands. All mutations use backend-owned adapters.' }, 'copy'),
          ],
        }, 'safety'),
      ],
    })

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
                          jsx(StatusDot, { tone: online ? 'good' : 'muted' }, 'dot'),
                          jsx(Badge, { variant: online ? 'default' : 'muted', children: online ? 'ONLINE' : 'OFFLINE' }, 'badge'),
                        ],
                      }, 'status'),
                      jsx('h1', { className: 'mt-3 text-2xl font-semibold tracking-tight', children: game.name }, 'name'),
                      jsx('p', { className: 'mt-2 max-w-2xl text-sm leading-6 text-muted-foreground', children: game.description || 'Game-specific controls managed by Hermes.' }, 'description'),
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
                  onValue: value => setCurrentValue(control, value),
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
                          onChange: event => setConfirmPhrase(event.target.value),
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

export default {
  id: ID,
  name: 'Game Host Console',
  defaultEnabled: false,
  register(ctx) {
    const GameHostPage = createGameHostPage(ctx)
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
        data: { path: ROUTE, label: 'Game Host', codicon: 'server-process' },
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'game-host-console.open',
          label: 'Open Game Host Console',
          keywords: ['game', 'server', 'minecraft', 'palworld', 'host'],
          run: () => host.navigate(ROUTE),
        },
      },
    ])
  },
}
