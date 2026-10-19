import { createContext, useContext } from 'react'

export const RuntimeConfigContext = createContext(null)

export function useRuntimeConfig() {
  return useContext(RuntimeConfigContext)
}
