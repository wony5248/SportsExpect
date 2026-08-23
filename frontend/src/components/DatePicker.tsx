import { useState } from 'react'
import { Box, Button, Drawer, Stack, Typography } from '@mui/material'
import CalendarMonthRounded from '@mui/icons-material/CalendarMonthRounded'
import CheckRounded from '@mui/icons-material/CheckRounded'
import CloseRounded from '@mui/icons-material/CloseRounded'
import { useMobile } from '../lib/useMobile'
import type { GameDate } from '../types'

/** Archive date picker. A native select is unreadable at this list length on a phone, so mobile
 *  gets a full-height sheet with real tap targets instead. */
export default function DatePicker({ dates, value, league, onChange }: {
  dates: GameDate[]
  value: string
  league: string
  onChange: (date: string) => void
}) {
  const mobile = useMobile()
  const [open, setOpen] = useState(false)
  const selected = dates.find((item) => item.date === value)
  const label = (item: GameDate) => `${item.date.replaceAll('-', '.')} · ${item.games}경기${
    league === 'ALL' ? ` (KBO ${item.kbo} / MLB ${item.mlb})` : ''}`

  if (!mobile) {
    return <select aria-label="저장된 시즌 경기일" value={selected ? value : ''}
      onChange={(event) => event.target.value && onChange(event.target.value)}>
      <option value="">경기 있는 날짜 선택</option>
      {dates.map((item) => <option key={item.date} value={item.date}>{label(item)}</option>)}
    </select>
  }

  return <>
    <Button className="date-sheet-trigger" onClick={() => setOpen(true)} startIcon={<CalendarMonthRounded />}>
      {selected ? label(selected) : '경기 있는 날짜 선택'}
    </Button>
    <Drawer anchor="bottom" open={open} onClose={() => setOpen(false)} className="date-sheet">
      <Stack direction="row" justifyContent="space-between" alignItems="center" className="date-sheet-head">
        <Box><b>경기 있는 날짜</b><span>{dates.length}일 저장됨</span></Box>
        <Button onClick={() => setOpen(false)} aria-label="닫기"><CloseRounded /></Button>
      </Stack>
      <Box className="date-sheet-list">
        {dates.map((item) => <button key={item.date} type="button"
          className={item.date === value ? 'selected' : ''}
          onClick={() => { onChange(item.date); setOpen(false) }}>
          <span>{item.date.replaceAll('-', '.')}</span>
          <small>{item.games}경기{league === 'ALL' ? ` · KBO ${item.kbo} / MLB ${item.mlb}` : ''}</small>
          {item.date === value && <CheckRounded />}
        </button>)}
        {!dates.length && <Typography className="date-sheet-empty">저장된 경기일이 없습니다.</Typography>}
      </Box>
    </Drawer>
  </>
}
