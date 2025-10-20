import { CommonModule, AsyncPipe } from '@angular/common';
import { Component, signal } from '@angular/core';
import { PlotlyModule } from 'angular-plotly.js';
import { BehaviorSubject, Observable, combineLatest, map, of, shareReplay, switchMap } from 'rxjs';
import * as PlotlyJS from 'plotly.js-dist-min';

import { PortfolioApiService } from '../../api/portfolio-api.service';
import {
  DividendsResponse,
  PortfolioSeries,
  ReturnsResponse,
  TradesResponse,
  UnrealizedResponse
} from '../../api/models';
import { environment } from '../../../environments/environment';
import { ThemeService, Theme } from '../../core/services/theme.service';

PlotlyModule.plotlyjs = PlotlyJS;

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, AsyncPipe, PlotlyModule],
  templateUrl: './dashboard.component.html',
  styleUrl: './dashboard.component.scss'
})
export class DashboardComponent {
  private readonly timeframe$ = new BehaviorSubject<Timeframe>('1M');
  readonly baseCurrency = environment.baseCurrency;
  private readonly currencyFormatter = new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency: this.baseCurrency,
    maximumFractionDigits: 2
  });
  readonly navSeries$: Observable<PortfolioSeries> = this.timeframe$.pipe(
    switchMap((timeframe) =>
      this.portfolioApi.getNavSeries(this.buildNavParams(timeframe)).pipe(shareReplay(1))
    )
  );
  readonly navData$ = combineLatest([
    this.navSeries$,
    this.themeService.theme$
  ]).pipe(map(([series, theme]) => ({ series, plot: this.toNavPlot(series, theme) })));

  readonly returnsPlots$ = this.timeframe$.pipe(
    switchMap((timeframe) => {
      const params = this.buildNavParams(timeframe);
      const data$ = this.portfolioApi
        .getReturnsSeries({ from: params.from, to: params.to, interval: params.interval })
        .pipe(shareReplay(1));
      return combineLatest([data$, this.themeService.theme$]).pipe(
        map(([res, theme]) => this.toReturnsPlots(res, theme))
      );
    })
  );

  readonly dividends$ = this.buildDividendRange().pipe(
    switchMap(({ from, to }) => this.portfolioApi.getDividends({ from, to })),
    map((response) => this.toDividendView(response))
  );

  readonly recentTrades$ = this.portfolioApi.getRecentTrades({ limit: 25 });

  readonly unrealized$: Observable<UnrealizedResponse> = this.portfolioApi.getUnrealized();

  constructor(
    private readonly portfolioApi: PortfolioApiService,
    private readonly themeService: ThemeService
  ) {}

  readonly timeframes: TimeframeOption[] = [
    { label: '1W', value: '1W' },
    { label: '1M', value: '1M' },
    { label: 'YTD', value: 'YTD' },
    { label: '1Y', value: '1Y' }
  ];

  readonly selectedTimeframe = signal<Timeframe>('1M');

  onSelectTimeframe(value: Timeframe): void {
    if (this.selectedTimeframe() === value) {
      return;
    }
    this.selectedTimeframe.set(value);
    this.timeframe$.next(value);
  }

  formatCurrency(value: number | null | undefined): string {
    return this.currencyFormatter.format(value ?? 0);
  }

  private toNavPlot(series: PortfolioSeries, theme: Theme): NavPlot | null {
    const colors = this.themeService.getColors(theme);
    const sorted = [...series.points].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    );
    if (!sorted.length) {
      return null;
    }

    const xValues = sorted.map((point) => point.timestamp);
    const yValues = sorted.map((point) => point.value);

    return {
      data: [
        {
          type: 'scatter',
          mode: 'lines',
          x: xValues,
          y: yValues,
          line: { color: colors.navLine, width: 2 },
          fill: 'tozeroy',
          fillcolor: colors.navFill,
          hovertemplate: '%{x}<br>%{y:.2f} ' + this.baseCurrency + '<extra></extra>'
        }
      ],
      layout: {
        margin: { l: 55, r: 20, t: 10, b: 40 },
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        autosize: true,
        height: 280,
        xaxis: {
          type: 'date',
          tickfont: { color: colors.axis },
          gridcolor: colors.grid
        },
        yaxis: {
          tickfont: { color: colors.axis },
          gridcolor: colors.grid,
          separatethousands: true
        }
      },
      config: {
        responsive: true,
        displayModeBar: false
      }
    };
  }

  private toReturnsPlots(response: ReturnsResponse, theme: Theme): ReturnsPlots | null {
    const colors = this.themeService.getColors(theme);
    const sorted = [...response.points].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    );
    if (!sorted.length) {
      return null;
    }

    const timestamps = sorted.map((point) => point.timestamp);
    const deltas = sorted.map((point) => point.delta);
    const absoluteColors = deltas.map((value) => (value >= 0 ? colors.positive : colors.negative));
    const percentValues = sorted.map((point) =>
      typeof point.return_pct === 'number' ? point.return_pct * 100 : null
    );

    const absolute: NavPlot = {
      data: [
        {
          type: 'bar',
          x: timestamps,
          y: deltas,
          marker: { color: absoluteColors },
          hovertemplate: '%{x}<br>%{y:.2f} ' + this.baseCurrency + '<extra></extra>'
        }
      ],
      layout: {
        margin: { l: 55, r: 20, t: 10, b: 40 },
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        autosize: true,
        height: 240,
        xaxis: {
          type: 'date',
          tickfont: { color: colors.axis },
          gridcolor: colors.grid
        },
        yaxis: {
          tickfont: { color: colors.axis },
          gridcolor: colors.grid,
          separatethousands: true
        }
      },
      config: { responsive: true, displayModeBar: false }
    };

    const percent: NavPlot = {
      data: [
        {
          type: 'scatter',
          mode: 'lines',
          x: timestamps,
          y: percentValues,
          line: { color: colors.percentLine, width: 2 },
          hovertemplate: '%{x}<br>%{y:.2f}%<extra></extra>'
        }
      ],
      layout: {
        margin: { l: 55, r: 20, t: 10, b: 40 },
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        autosize: true,
        height: 240,
        xaxis: {
          type: 'date',
          tickfont: { color: colors.axis },
          gridcolor: colors.grid
        },
        yaxis: {
          tickfont: { color: colors.axis },
          gridcolor: colors.grid,
          separatethousands: false,
          ticksuffix: '%'
        }
      },
      config: { responsive: true, displayModeBar: false }
    };

    return { absolute, percent };
  }

  private toDividendView(response: DividendsResponse): DividendView {
    const entries = response.dividends
      .map((entry) => ({
        paymentDate: new Date(entry.payment_date),
        accountId: entry.account_id,
        amount: entry.amount,
        amountBase: entry.amount_base,
        currency: entry.currency,
        description: entry.description || 'Dividend'
      }))
      .sort((a, b) => b.paymentDate.getTime() - a.paymentDate.getTime());

    return {
      entries,
      totalAmountBase: response.total_amount_base
    };
  }

  private buildDividendRange(): Observable<{ from: string; to: string }> {
    const now = new Date();
    const from = new Date(now.getTime() - 365 * 24 * 60 * 60 * 1000);
    return of({ from: from.toISOString(), to: now.toISOString() });
  }

  private buildNavParams(timeframe: Timeframe): {
    from: string;
    to: string;
    interval: string;
  } {
    const now = new Date();
    const to = now.toISOString();

    let fromDate: Date;
    switch (timeframe) {
      case '1W':
        fromDate = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
        break;
      case '1M':
        fromDate = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
        break;
      case 'YTD':
        fromDate = new Date(now.getFullYear(), 0, 1);
        break;
      case '1Y':
        fromDate = new Date(now.getTime() - 365 * 24 * 60 * 60 * 1000);
        break;
      default:
        fromDate = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
    }

    const spanDays = (now.getTime() - fromDate.getTime()) / (24 * 60 * 60 * 1000);
    const interval = spanDays <= 31 ? '1h' : '1d';

    return {
      from: fromDate.toISOString(),
      to,
      interval
    };
  }
}

type Timeframe = '1W' | '1M' | 'YTD' | '1Y';

interface TimeframeOption {
  label: string;
  value: Timeframe;
}

interface NavPlot {
  data: any[];
  layout: any;
  config: any;
}

interface ReturnsPlots {
  absolute: NavPlot;
  percent: NavPlot;
}

interface DividendRow {
  paymentDate: Date;
  accountId: string;
  amount: number;
  amountBase: number;
  currency: string;
  description: string;
}

interface DividendView {
  entries: DividendRow[];
  totalAmountBase: number;
}
