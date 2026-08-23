import { useState } from 'react'
import { Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, TextField, Typography } from '@mui/material'
import LoginRounded from '@mui/icons-material/LoginRounded'
import { authConfigured, sendMagicLink, signInWithPassword } from '../lib/auth'
import type { AuthSession } from '../lib/auth'
import { useMobile } from '../lib/useMobile'


export default function LoginDialog({ open, onClose, onSignedIn }: {
  open: boolean
  onClose: () => void
  onSignedIn: (session: AuthSession) => void
}) {
  const mobile = useMobile()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const submit = async () => {
    if (!email || !password) { setError('이메일과 비밀번호를 입력하세요.'); return }
    setBusy(true); setError(null); setNotice(null)
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

  const sendLink = async () => {
    if (!email) { setError('초대받은 이메일 주소를 입력하세요.'); return }
    setBusy(true); setError(null); setNotice(null)
    try {
      await sendMagicLink(email.trim())
      setNotice('로그인 링크를 보냈습니다. 이메일에서 링크를 눌러 이 사이트로 돌아오세요.')
    } catch (err) {
      setError(err instanceof Error ? err.message : '로그인 링크를 보내지 못했습니다.')
    } finally {
      setBusy(false)
    }
  }

  return <Dialog open={open} onClose={busy ? undefined : onClose} fullWidth maxWidth="xs" fullScreen={mobile} className="app-dialog">
    <DialogTitle><Stack direction="row" spacing={1} alignItems="center"><LoginRounded /><span>사용자 로그인</span></Stack></DialogTitle>
    <DialogContent>
      <Stack spacing={2} sx={{ pt: 1 }}>
        <Typography className="settings-copy">초대받은 이메일을 입력하고 로그인 링크를 다시 받을 수 있습니다. 비밀번호를 따로 설정한 계정은 비밀번호 로그인도 사용할 수 있습니다.</Typography>
        {!authConfigured() && <Alert severity="error">Supabase 로그인 환경변수가 설정되지 않았습니다.</Alert>}
        <TextField label="이메일" type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" fullWidth />
        <TextField label="비밀번호(선택)" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" fullWidth
          helperText="초대 링크로 가입했고 비밀번호를 설정하지 않았다면 비워 두고 로그인 링크 받기를 누르세요."
          onKeyDown={(event) => { if (event.key === 'Enter') void (password ? submit() : sendLink()) }} />
        {error && <Alert severity="error">{error}</Alert>}
        {notice && <Alert severity="success">{notice}</Alert>}
      </Stack>
    </DialogContent>
    <DialogActions sx={{ flexWrap: 'wrap', gap: 1 }}>
      <Button onClick={onClose} disabled={busy}>닫기</Button>
      <Button variant="outlined" onClick={() => void sendLink()} disabled={busy || !authConfigured()}>이메일 로그인 링크 받기</Button>
      <Button variant="contained" onClick={() => void submit()} disabled={busy || !authConfigured() || !password}>비밀번호 로그인</Button>
    </DialogActions>
  </Dialog>
}
