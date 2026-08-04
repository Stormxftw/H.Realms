declare module '@hermes/plugin-sdk' {
  export type PluginContext = any
  export type HermesPlugin = any
  export const Badge: any
  export const Button: any
  export const ConfirmDialog: any
  export const ErrorState: any
  export const GlyphSpinner: any
  export const Input: any
  export const PALETTE_AREA: any
  export const ROUTES_AREA: any
  export const ScrollArea: any
  export const Select: any
  export const SelectContent: any
  export const SelectItem: any
  export const SelectTrigger: any
  export const SelectValue: any
  export const Separator: any
  export const SIDEBAR_NAV_AREA: any
  export const Skeleton: any
  export const StatusDot: any
  export const Switch: any
  export const host: any
  export const useMutation: any
  export const usePluginI18n: any
  export const useQuery: any
  export const useQueryClient: any
  export const useValue: any
}

declare module 'react' {
  export type ReactNode = unknown
  export type ChangeEvent<T extends { value?: unknown }> = { target: T }
  export function useState<T>(initializer: () => T): [T, (value: T | ((previous: T) => T)) => void]
  export function useState<T>(initial: T): [T, (value: T | ((previous: T) => T)) => void]
}

declare module 'react/jsx-runtime' {
  export function jsx(type: any, props: any, key?: any): any
}
