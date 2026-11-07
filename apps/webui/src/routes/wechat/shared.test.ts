import { afterEach, describe, expect, it, vi } from 'vitest'
import { waitForCommand } from './shared'

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
const response = (data: unknown) => ({ok:true,status:200,json:async () => ({code:0,data})})

describe('Windows command acceptance', () => {
  it('keeps reading one acknowledged command until the actual terminal receipt', async () => {
    vi.useFakeTimers()
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({id:'command-one',status:'issued'}))
      .mockResolvedValueOnce(response({id:'command-one',status:'succeeded',detail:{status:'opened'}}))
    vi.stubGlobal('fetch',fetch)
    const pending = waitForCommand({id:'command-one',action:'open_contact',status:'queued'})
    await vi.advanceTimersByTimeAsync(1500)
    expect((await pending).status).toBe('succeeded')
    expect(fetch.mock.calls.map(call => call[0])).toEqual(['/api/wemaa/commands/command-one','/api/wemaa/commands/command-one'])
    expect(fetch.mock.calls.every(call => !call[1].method)).toBe(true)
  })

  it.each(['failed','expired','uncertain'])('does not promote %s into success or resubmit it', async status => {
    const fetch=vi.fn().mockResolvedValue(response({id:'one',status,detail:{reason:'NOT_VERIFIED'}}))
    vi.stubGlobal('fetch',fetch)
    await expect(waitForCommand({id:'one',action:'set_group_whitelist',status:'queued'})).rejects.toThrow('NOT_VERIFIED')
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('stops on an unknown network outcome without resubmitting', async () => {
    const fetch=vi.fn().mockRejectedValue(new Error('Network interrupted'))
    vi.stubGlobal('fetch',fetch)
    await expect(waitForCommand({id:'one',action:'open_contact',status:'queued'})).rejects.toThrow('Network interrupted')
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
