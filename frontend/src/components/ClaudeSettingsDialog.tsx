import { useState } from 'react'
import {
  Alert, Box, Button, Dialog, DialogActions, DialogContent, DialogTitle,
  FormControlLabel, MenuItem, Stack, Switch, TextField, Typography,
} from '@mui/material'
import KeyRounded from '@mui/icons-material/KeyRounded'
import DeleteOutlineRounded from '@mui/icons-material/DeleteOutlineRounded'
import { fetchClaudeKeyStatus, fetchClaudeModels, registerClaudeKey, removeClaudeKey } from '../lib/api'
import type { ClaudeKeyStatus, ClaudeModel } from '../types'


export default function ClaudeSettingsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [adminToken, setAdminToken] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [status, setStatus] = useState<ClaudeKeyStatus | null>(null)
  const [models, setModels] = useState<ClaudeModel[]>([])
  const [selectedModel, setSelectedModel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const run = async (operation: () => Promise<ClaudeKeyStatus>, success: string) => {
    setBusy(true); setError(null); setNotice(null)
    try {
      const next = await operation()
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
    if (!adminToken) { setError('관리자 토큰을 입력하세요.'); return }
    const next = await run(() => fetchClaudeKeyStatus(adminToken), '현재 서버 설정을 확인했습니다.')
    if (next) {
      setEnabled(next.enabled)
      setSelectedModel(next.model)
    }
  }

  const loadModels = async () => {
    if (!adminToken) { setError('관리자 토큰을 입력하세요.'); return }
    if (!apiKey && !status?.configured) {
      setError('처음 연결할 때는 Claude API 키를 입력하세요.')
      return
    }
    setBusy(true); setError(null); setNotice(null)
    try {
      const available = await fetchClaudeModels(adminToken, apiKey || undefined)
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
    if (!adminToken || !selectedModel) { setError('관리자 토큰을 확인하고 Claude 모델을 선택하세요.'); return }
    if (!apiKey && !status?.configured) { setError('처음 연결할 때는 Claude API 키를 입력하세요.'); return }
    const next = await run(
      () => registerClaudeKey(adminToken, apiKey || null, selectedModel, enabled),
      '키와 모델 설정을 저장했습니다. 다음 예측 갱신부터 적용됩니다.',
    )
    if (next) setApiKey('')
  }

  const remove = async () => {
    if (!adminToken) { setError('관리자 토큰을 입력하세요.'); return }
    if (!window.confirm('UI에서 등록한 Claude API 키를 제거할까요?')) return
    await run(() => removeClaudeKey(adminToken), 'UI 등록 키와 모델 설정을 제거했습니다.')
    setApiKey('')
    setModels([])
    setSelectedModel('')
  }

  return <Dialog open={open} onClose={busy ? undefined : onClose} fullWidth maxWidth="sm">
    <DialogTitle><Stack direction="row" spacing={1} alignItems="center"><KeyRounded /><span>Claude API 연결</span></Stack></DialogTitle>
    <DialogContent>
      <Stack spacing={2} sx={{ pt: 1 }}>
        <Typography className="settings-copy">관리자만 키를 등록할 수 있습니다. Claude 키는 브라우저 저장소에 남기지 않고 서버에서 암호화해 저장하며, 화면에는 다시 표시하지 않습니다.</Typography>
        <TextField
          label="관리자 토큰" type="password" value={adminToken}
          onChange={(event) => setAdminToken(event.target.value)} autoComplete="off" fullWidth
        />
        <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1}>
          <Button variant="outlined" onClick={() => void inspect()} disabled={busy || !adminToken}>현재 상태 확인</Button>
          <Button variant="outlined" onClick={() => void loadModels()} disabled={busy || !adminToken || (!apiKey && !status?.configured)}>키 인증 · 모델 불러오기</Button>
          {status && <Box className={`claude-key-state ${status.enabled ? 'enabled' : ''}`}>
            <b>{status.configured ? status.enabled ? '연결 활성' : '키 등록 · 비활성' : '키 미등록'}</b>
            <small>{status.configured ? `${status.model} · ${status.source === 'admin_ui' ? 'UI 등록' : '환경변수'} · ${status.fingerprint}` : status.model}</small>
          </Box>}
        </Stack>
        <TextField
          label="Claude API 키" type="password" value={apiKey}
          onChange={(event) => setApiKey(event.target.value)} autoComplete="new-password" fullWidth
          placeholder="sk-ant-..."
          helperText="저장 전에 Anthropic Models API로 인증을 확인합니다. 이 확인에는 예측 토큰 비용이 발생하지 않습니다."
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
        <FormControlLabel control={<Switch checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />} label="다음 예측 갱신부터 Claude 보조 분석 사용" />
        {status?.configured_model_available === false && <Alert severity="warning">현재 API 키로 저장된 모델 {status.model}을 사용할 수 없습니다. 모델 목록을 다시 불러와 선택하세요.</Alert>}
        {error && <Alert severity="error">{error}</Alert>}
        {notice && <Alert severity="success">{notice}</Alert>}
      </Stack>
    </DialogContent>
    <DialogActions sx={{ justifyContent: 'space-between', px: 3, pb: 2 }}>
      <Button color="error" startIcon={<DeleteOutlineRounded />} onClick={() => void remove()} disabled={busy || status?.source !== 'admin_ui'}>UI 등록 키 제거</Button>
      <Stack direction="row" spacing={1}>
        <Button onClick={onClose} disabled={busy}>닫기</Button>
        <Button variant="contained" onClick={() => void save()} disabled={busy || !adminToken || !selectedModel || (!apiKey && !status?.configured)}>설정 저장</Button>
      </Stack>
    </DialogActions>
  </Dialog>
}
