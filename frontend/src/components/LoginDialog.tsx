import { useState } from 'react'
import { Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, TextField, Typography } from '@mui/material'
import LoginRounded from '@mui/icons-material/LoginRounded'
import { authConfigured, signInWithPassword } from '../lib/auth'
import type { AuthSession } from '../lib/auth'


export default function LoginDialog({ open, onClose, onSignedIn }: {
  open: boolean
  onClose: () => void
  onSignedIn: (session: AuthSession) => void
}) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    if (!email || !password) { setError('이메일과 비밀번호를 입력하세요.'); return }
    setBusy(true); setError(null)
    try {
      const session = await signInWithPassword(email.trim(), password)
      setPassword('')
      onSignedIn(session)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '로그인하지 못했습니다.')
    } finally {
      setBusy(false)
    }
  }

  return <Dialog open={open} onClose={busy ? undefined : onClose} fullWidth maxWidth="xs">
    <DialogTitle><Stack direction="row" spacing={1} alignItems="center"><LoginRounded /><span>사용자 로그인</span></Stack></DialogTitle>
    <DialogContent>
      <Stack spacing={2} sx={{ pt: 1 }}>
        <Typography className="settings-copy">등록된 4명의 계정으로 로그인합니다. 로그인한 사용자의 Claude API 키와 개인 분석은 다른 사용자와 분리됩니다.</Typography>
        {!authConfigured() && <Alert severity="error">Supabase 로그인 환경변수가 설정되지 않았습니다.</Alert>}
        <TextField label="이메일" type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" fullWidth />
        <TextField label="비밀번호" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" fullWidth
          onKeyDown={(event) => { if (event.key === 'Enter') void submit() }} />
        {error && <Alert severity="error">{error}</Alert>}
      </Stack>
    </DialogContent>
    <DialogActions>
      <Button onClick={onClose} disabled={busy}>닫기</Button>
      <Button variant="contained" onClick={() => void submit()} disabled={busy || !authConfigured()}>로그인</Button>
    </DialogActions>
  </Dialog>
}
