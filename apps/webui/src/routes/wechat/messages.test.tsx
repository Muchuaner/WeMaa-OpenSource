import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MessageTimeline } from './messages'

vi.mock('./shared', () => ({
  Evidence: ({alt}: {alt: string}) => <span role="img" aria-label={alt} />,
  Empty: ({children}: {children: React.ReactNode}) => <p>{children}</p>,
  Stamp: ({value}: {value: number}) => <span>{value}</span>,
}))

describe('WeChat timeline parity', () => {
  it('keeps time separators and sender labels out of message bubbles', () => {
    const {container} = render(<MessageTimeline rows={[
      {payload: {message_kind:'time', text:'星期一 20:29'}},
      {payload: {message_kind:'sender_label', text:'only an OCR label'}},
      {payload: {message_kind:'time', text:'星期一 20:29'}},
      {payload: {direction:'incoming', message_kind:'text', text:'actual message'}},
      {payload: {direction:'unknown', message_kind:'unassigned', text:'unassigned OCR'}},
    ]} />)
    expect(container.querySelectorAll('[data-message-direction]')).toHaveLength(1)
    expect(screen.getAllByText('星期一 20:29')).toHaveLength(1)
    expect(within(screen.getByRole('log')).queryByText('only an OCR label')).toBeNull()
    expect(screen.getByText('1 条未归属识别记录')).toBeInTheDocument()
  })

  it('does not turn an unverified wxid or group label into a proven identity', () => {
    render(<MessageTimeline rows={[{payload: {direction:'incoming', text:'body',
      sender_wechat_id:'unproven-id', sender_identity_verified:true, sender_identity_status:'visual_only',
      sender_identity_key:'visual-key-123456789', group_sender_name:'unproven-name', group_sender_verified:false}}]} />)
    expect(screen.getByText('发言人待核验')).toBeInTheDocument()
    expect(screen.queryByText('微信号：unproven-id')).toBeNull()
    expect(screen.getByText('视觉身份：visual-key-1…')).toBeInTheDocument()
  })

  it('displays independently verified identity with the actual sender avatar', () => {
    render(<MessageTimeline rows={[{payload: {direction:'outgoing', text:'body', sender_name:'verified speaker',
      sender_identity_verified:true, sender_identity_status:'profile_verified', sender_wechat_id:'verified-id'}}]} />)
    expect(screen.getByText('微信号：verified-id')).toBeInTheDocument()
    expect(screen.getByRole('img', {name:'verified speaker的头像'})).toBeInTheDocument()
  })
})
