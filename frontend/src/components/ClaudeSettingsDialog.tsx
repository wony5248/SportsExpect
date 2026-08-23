import { useEffect, useState } from 'react'
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, MenuItem, Stack, Switch, TextField, Typography,
} from '@mui/material'
import KeyRounded from '@mui/icons-material/KeyRounded'
import DeleteOutlineRounded from '@mui/icons-material/DeleteOutlineRounded'
import { useMobile } from '../lib/useMobile'
import { fetchClaudeKeyStatus, fetchClaudeModels, registerClaudeKey, removeClaudeKey } from '../lib/api'
import { getAccessToken } from '../lib/auth'
import type { ClaudeKeyStatus, ClaudeModel } from '../types'


export default function ClaudeSettingsDialog({ open, email, onClose }: {
  open: boolean
  email: string | null
  onClose: () => void
}) {
  const mobile = useMobile()
  const [apiKey, setApiKey] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [status, setStatus] = useState<ClaudeKeyStatus | null>(null)
  const [models, setModels] = useState<ClaudeModel[]>([])
  const [selectedModel, setSelectedModel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const run = async (operation: (token: string) => Promise<ClaudeKeyStatus>, success: string) => {
    setBusy(true); setError(null); setNotice(null)
    try {
      const next = await operation(await getAccessToken())
      setStatus(next); setNotice(success)
      return next
    } catch (err) {
      setError(err instanceof Error ? err.message : '요청을 처리하지 못했습니다.')
      return null
    } finally {
      setBusy(false)
    }
  }

  const inspect = async () => {
    const next = await run(fetchClaudeKeyStatus, '내 Claude 설정을 확인했습니다.')
    if (next) {
      setEnabled(next.enabled)
      setSelectedModel(next.model)
    }
  }

  useEffect(() => {
    if (open) void inspect()
    // Opening is the only automatic fetch boundary; inspect is intentionally local to this dialog.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const loadModels = async () => {
    if (!apiKey && !status?.configured) {
      setError('처음 연결할 때는 Claude API 키를 입력하세요.')
      return
    }
    setBusy(true); setError(null); setNotice(null)
    try {
      const available = await fetchClaudeModels(await getAccessToken(), apiKey || undefined)
      setModels(available)
      const preferred = [selectedModel, status?.model, 'claude-sonnet-5']
        .find((candidate) => candidate && available.some((model) => model.id === candidate))
      setSelectedModel(preferred ?? available[0]?.id ?? '')
      setNotice(`${available.length}개의 사용 가능한 Claude 모델을 확인했습니다.`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '모델 목록을 불러오지 못했습니다.')
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    if (!selectedModel) { setError('Claude 모델을 선택하세요.'); return }
    if (!apiKey && !status?.configured) { setError('처음 연결할 때는 Claude API 키를 입력하세요.'); return }
    const next = await run(
      (token) => registerClaudeKey(token, apiKey || null, selectedModel, enabled),
      '내 키와 모델 설정을 저장했습니다.',
    )
    if (next) setApiKey('')
  }

  const remove = async () => {
    if (!window.confirm('내 Claude API 키를 제거할까요?')) return
    await run(removeClaudeKey, '내 Claude 키와 모델 설정을 제거했습니다.')
    setApiKey('')
    setModels([])
    setSelectedModel('')
  }

  return <Dialog open={open} onClose={busy ? undefined : onClose} fullWidth maxWidth="sm" fullScreen={mobile} className="app-dialog">
    <DialogTitle><Stack direction="row" spacing={1} alignItems="center"><KeyRounded /><span>내 Claude API 연결</span></Stack></DialogTitle>
    <DialogContent>
      <Stack spacing={2} sx={{ pt: 1 }}>
        <Typography className="settings-copy">{email ?? '로그인 사용자'} 계정 전용 설정입니다. 키는 서버에 암호화해 저장하고 공용 예측이나 다른 사용자 화면에는 사용하지 않습니다.</Typography>
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
          <Button variant="outlined" onClick={() => void inspect()} disabled={busy}>현재 상태 확인</Button>
          <Button variant="outlined" onClick={() => void loadModels()} disabled={busy || (!apiKey && !status?.configured)}>키 인증 · 모델 불러오기</Button>
          {status && <Box className={`claude-key-state ${status.enabled ? 'enabled' : ''}`}>
            <b>{status.configured ? status.enabled ? '개인 연결 활성' : '키 등록 · 비활성' : '키 미등록'}</b>
            <small>{status.configured ? `${status.model} · ${status.fingerprint}` : '개인 Claude 키를 연결하세요'}</small>
          </Box>}
        </Stack>
        <TextField
          label="내 Claude API 키" type="password" value={apiKey}
          onChange={(event) => setApiKey(event.target.value)} autoComplete="new-password" fullWidth
          placeholder="sk-ant-..."
          helperText="서버 저장 전에 Anthropic Models API로 인증합니다. 원문 키는 다시 표시하지 않습니다."
        />
        <TextField
          select label="Claude 모델" value={selectedModel}
          onChange={(event) => setSelectedModel(event.target.value)} fullWidth
          disabled={busy || models.length === 0}
          helperText={models.length ? '이 API 키로 실제 사용할 수 있는 모델만 표시합니다.' : '키 인증 · 모델 불러오기를 먼저 실행하세요.'}
        >
          {models.map((model) => <MenuItem key={model.id} value={model.id}>
            {model.display_name} ({model.id})
          </MenuItem>)}
        </TextField>
        <FormControlLabel control={<Switch checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />} label="내 경기 카드에서 Claude 개인 분석 사용" />
        {status?.configured_model_available === false && <Alert severity="warning">현재 API 키로 저장된 모델 {status.model}을 사용할 수 없습니다. 모델 목록을 다시 불러와 선택하세요.</Alert>}
        {error && <Alert severity="error">{error}</Alert>}
        {notice && <Alert severity="success">{notice}</Alert>}
      </Stack>
    </DialogContent>
    <DialogActions sx={{ justifyContent: 'space-between', px: 3, pb: 2 }}>
      <Button color="error" startIcon={<DeleteOutlineRounded />} onClick={() => void remove()} disabled={busy || !status?.configured}>내 키 제거</Button>
      <Stack direction="row" spacing={1}>
        <Button onClick={onClose} disabled={busy}>닫기</Button>
        <Button variant="contained" onClick={() => void save()} disabled={busy || !selectedModel || (!apiKey && !status?.configured)}>설정 저장</Button>
      </Stack>
    </DialogActions>
  </Dialog>
}
