export interface TimeSeriesPoint {
  timestamp: string;
  value: number;
}

export interface PortfolioSeries {
  points: TimeSeriesPoint[];
}

export interface ExposureSlice {
  label: string;
  value_eur: number;
  weight: number;
}

export interface ExposureResponse {
  slices: ExposureSlice[];
  total_eur: number;
}

export interface UnrealizedItem {
  symbol: string;
  name?: string;
  market_value_eur?: number;
  unrealized_pnl_eur: number;
}

export interface UnrealizedResponse {
  items: UnrealizedItem[];
}

export interface PortfolioPosition {
  account_id: string;
  symbol: string;
  name?: string;
  shares: number;
  currency: string;
  market_value_eur?: number;
  cost_basis_eur?: number;
  unrealized_pnl_eur?: number;
  weight?: number;
  last_price?: number;
  last_price_as_of?: string;
}

export interface PositionsResponse {
  positions: PortfolioPosition[];
  total_eur: number;
}

export interface RecentTrade {
  executed_at: string;
  account_id: string;
  symbol: string;
  trade_type: string;
  quantity: number;
  price: number;
  currency: string;
  net_amount: number;
  fees: number;
}

export interface TradesResponse {
  trades: RecentTrade[];
}

export interface DividendEntry {
  payment_date: string;
  account_id: string;
  amount: number;
  amount_base: number;
  currency: string;
  description?: string;
  fx_rate?: number;
}

export interface DividendsResponse {
  dividends: DividendEntry[];
  total_amount_base: number;
}

export interface ReturnPoint {
  timestamp: string;
  nav: number;
  delta: number;
  return_pct?: number;
}

export interface ReturnsResponse {
  points: ReturnPoint[];
}
