import { inject, Injectable } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';

import { environment } from '../../environments/environment';
import {
  ExposureResponse,
  PortfolioSeries,
  PositionsResponse,
  DividendsResponse,
  ReturnsResponse,
  TradesResponse,
  UnrealizedResponse,
  ReturnsOverviewResponse,
  UnmappedInstrumentsResponse,
  MappedInstrumentsResponse,
  YFinanceSearchResponse,
  InstrumentMappingResponse
} from './models';

@Injectable({
  providedIn: 'root'
})
export class PortfolioApiService {
  private readonly http = inject(HttpClient);
  private readonly apiBase = environment.apiBaseUrl;

  getNavSeries(params?: {
    from?: string;
    to?: string;
    interval?: string;
    accountId?: string;
  }): Observable<PortfolioSeries> {
    const query = this.composeParams(params);
    return this.http.get<PortfolioSeries>(`${this.apiBase}/portfolio/value`, {
      params: query
    });
  }

  getUnrealized(accountId?: string): Observable<UnrealizedResponse> {
    const params = this.composeParams({ accountId });
    return this.http.get<UnrealizedResponse>(`${this.apiBase}/portfolio/unrealized`, {
      params
    });
  }

  getExposure(
    dimension: 'country' | 'sector' | 'currency' | 'region' | 'industry',
    accountId?: string
  ): Observable<ExposureResponse> {
    const params = this.composeParams({ accountId });
    return this.http.get<ExposureResponse>(`${this.apiBase}/portfolio/exposure/${dimension}`, {
      params
    });
  }

  getPositions(accountId?: string): Observable<PositionsResponse> {
    const params = this.composeParams({ accountId });
    return this.http.get<PositionsResponse>(`${this.apiBase}/portfolio/positions`, {
      params
    });
  }

  getRecentTrades(params?: { accountId?: string; limit?: number }): Observable<TradesResponse> {
    const query = this.composeParams({ accountId: params?.accountId, limit: params?.limit });
    return this.http.get<TradesResponse>(`${this.apiBase}/transactions/recent`, {
      params: query
    });
  }

  getDividends(params?: {
    from?: string;
    to?: string;
    accountId?: string;
  }): Observable<DividendsResponse> {
    const query = this.composeParams({
      from: params?.from,
      to: params?.to,
      accountId: params?.accountId
    });
    return this.http.get<DividendsResponse>(`${this.apiBase}/portfolio/dividends`, {
      params: query
    });
  }

  getReturnsSeries(params?: {
    from?: string;
    to?: string;
    interval?: string;
    accountId?: string;
  }): Observable<ReturnsResponse> {
    const query = this.composeParams({
      from: params?.from,
      to: params?.to,
      interval: params?.interval,
      accountId: params?.accountId
    });
    return this.http.get<ReturnsResponse>(`${this.apiBase}/portfolio/returns`, {
      params: query
    });
  }

  getReturnsOverview(params?: {
    from?: string;
    to?: string;
    accountId?: string;
  }): Observable<ReturnsOverviewResponse> {
    const query = this.composeParams({
      from: params?.from,
      to: params?.to,
      accountId: params?.accountId
    });
    return this.http.get<ReturnsOverviewResponse>(
      `${this.apiBase}/portfolio/returns/overview`,
      {
        params: query
      }
    );
  }

  getUnmappedInstruments(): Observable<UnmappedInstrumentsResponse> {
    return this.http.get<UnmappedInstrumentsResponse>(
      `${this.apiBase}/instruments/unmapped`
    );
  }

  getMappedInstruments(): Observable<MappedInstrumentsResponse> {
    return this.http.get<MappedInstrumentsResponse>(`${this.apiBase}/instruments/mapped`);
  }

  searchYfinanceSymbols(query: string, limit = 10): Observable<YFinanceSearchResponse> {
    const params = new HttpParams().set('q', query).set('limit', limit);
    return this.http.get<YFinanceSearchResponse>(`${this.apiBase}/instruments/search`, {
      params
    });
  }

  updateInstrumentMapping(
    instrumentId: number,
    yfinanceSymbol: string | null
  ): Observable<InstrumentMappingResponse> {
    return this.http.put<InstrumentMappingResponse>(
      `${this.apiBase}/instruments/${instrumentId}/mapping`,
      { yfinance_symbol: yfinanceSymbol }
    );
  }

  private composeParams(input?: {
    from?: string;
    to?: string;
    interval?: string;
    accountId?: string;
    limit?: number;
  }): HttpParams {
    let params = new HttpParams();
    if (!input) {
      return params;
    }
    if (input.from) {
      params = params.set('from', input.from);
    }
    if (input.to) {
      params = params.set('to', input.to);
    }
    if (input.interval) {
      params = params.set('interval', input.interval);
    }
    if (input.accountId) {
      params = params.set('account_id', input.accountId);
    }
    if (typeof input.limit === 'number') {
      params = params.set('limit', input.limit);
    }
    return params;
  }
}
