/** Dashboard 接口。 */

import request from '@/api/request'
import type { TicketBrief } from '@/types/ticket'

export interface DashboardOverview {
  total: number
  today_created: number
  today_resolved: number
  pending: number
  processing: number
  resolved: number
  closed: number
  overdue: number
  at_risk: number
  unassigned: number
}

export interface TrendPoint {
  date: string
  created: number
  resolved: number
}

export interface NameCount {
  name: string
  value: number
}

export interface DistributionResponse {
  by_status: NameCount[]
  by_priority: NameCount[]
  by_category: NameCount[]
}

export interface SlaRiskResponse {
  total_overdue: number
  total_at_risk: number
  items: TicketBrief[]
}

export interface WorkloadItem {
  user_id: number
  username: string
  processing: number
  resolved: number
  total: number
}

export function getOverview(): Promise<DashboardOverview> {
  return request.get('/dashboard/overview')
}

export function getTrend(days = 7): Promise<{ days: number; points: TrendPoint[] }> {
  return request.get('/dashboard/trend', { params: { days } })
}

export function getDistribution(): Promise<DistributionResponse> {
  return request.get('/dashboard/distribution')
}

export function getSlaRisk(limit = 10): Promise<SlaRiskResponse> {
  return request.get('/dashboard/sla-risk', { params: { limit } })
}

export function getWorkload(limit = 10): Promise<WorkloadItem[]> {
  return request.get('/dashboard/workload', { params: { limit } })
}
