import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { I18nContext } from '../i18n'
import ProviderDataNotice from './ProviderDataNotice'

describe('Provider data destination disclosure', () => {
  it('shows the configured origin and reported region without exposing URL credentials or private paths', () => {
    const { container } = render(<ProviderDataNotice provider={{ base_url: 'https://user:private-password@gateway.example:8443/private-path?key=private-query#private-fragment', region: 'EU per contract' }} />)
    expect(screen.getByText('Endpoint: https://gateway.example:8443')).toBeInTheDocument()
    expect(screen.getByText('Reported region: EU per contract')).toBeInTheDocument()
    expect(screen.getByText(/Model requests may cross borders/)).toBeVisible()
    expect(container).not.toHaveTextContent(/private-password|private-path|private-query|private-fragment/)
    expect(screen.getByText(/does not verify processing or storage locations/)).toBeInTheDocument()
    expect(screen.getByText(/connected tools can also send task data/)).toBeInTheDocument()
  })

  it.each(['not-an-endpoint-private', 'javascript:private', 'file:///private', ' '])('keeps invalid destinations unknown instead of showing raw content (%s)', (base_url) => {
    render(<ProviderDataNotice provider={{ base_url, region: 'unspecified' }} />)
    expect(screen.getByText('Endpoint: Endpoint not recognized')).toBeInTheDocument()
    expect(screen.getByText('Reported region: Not specified')).toBeInTheDocument()
    expect(screen.queryByText(/private/)).not.toBeInTheDocument()
  })

  it('distinguishes Auto routing from missing selected-Provider information', () => {
    const { rerender } = render(<ProviderDataNotice automatic />)
    expect(screen.getByText(/Auto uses server-side Provider selection/)).toBeVisible()
    expect(screen.queryByText(/Reported region:/)).not.toBeInTheDocument()
    rerender(<ProviderDataNotice />)
    expect(screen.getByText(/selected Provider details are unavailable/)).toBeVisible()
    expect(screen.queryByText(/Auto uses/)).not.toBeInTheDocument()
  })

  it('updates a changed configuration and treats environment metadata as unverified in Chinese', () => {
    const { rerender } = render(<I18nContext.Provider value="zh-CN"><ProviderDataNotice provider={{ base_url: null, region: 'configured environment' }} /></I18nContext.Provider>)
    expect(screen.getByText('管理员填写的区域：未注明')).toBeVisible()
    expect(screen.getByText(/模型请求可能涉及跨境传输/)).toBeVisible()
    rerender(<I18nContext.Provider value="zh-CN"><ProviderDataNotice provider={{ base_url: 'http://127.0.0.1:3000/proxy', region: '区域 A' }} /></I18nContext.Provider>)
    expect(screen.getByText('端点：http://127.0.0.1:3000')).toBeVisible()
    expect(screen.getByText('管理员填写的区域：区域 A')).toBeVisible()
    expect(screen.queryByText(/管理员填写的区域：未注明/)).not.toBeInTheDocument()
    expect(screen.getByText(/代理端点可能继续转发请求/)).toBeInTheDocument()
  })
})
