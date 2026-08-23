import { useMediaQuery } from '@mui/material'

/** True on phone-sized screens, where dialogs take the whole viewport instead of floating. */
export function useMobile() {
  return useMediaQuery('(max-width: 700px)')
}
