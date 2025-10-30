import { CommonModule, AsyncPipe } from '@angular/common';
import { Component, signal } from '@angular/core';
import { PlotlyModule } from 'angular-plotly.js';
import { BehaviorSubject, combineLatest, map, shareReplay, switchMap } from 'rxjs';
import * as PlotlyJS from 'plotly.js-dist-min';

import { PortfolioApiService } from '../../api/portfolio-api.service';
import { ReturnsOverviewResponse, ReturnsResponse } from '../../api/models';
import { ThemeService, Theme } from '../../core/services/theme.service';
import { environment } from '../../../environments/environment';

PlotlyModule.plotlyjs = PlotlyJS;

@Component({
  selector: 'app-returns',
  standalone: true,
  imports: [CommonModule, AsyncPipe, PlotlyModule],
  templateUrl: './returns.component.html',
  styleUrl: './returns.component.scss'
})
export class ReturnsComponent {
  private readonly timeframe$ = new BehaviorSubject<Timeframe>('1Y');
  readonly selectedTimeframe = signal<Timeframe>('1Y');
  readonly baseCurrency = environment.baseCurrency;
  private readonly percentFormatters = new Map<string, Intl.NumberFormat>();

  readonly timeframes: TimeframeOption[] = [
    { label: '1M', value: '1M' },
    { label: '3M', value: '3M' },
    { label: 'YTD', value: 'YTD' },
    { label: '1Y', value: '1Y' }
  ];

  readonly overview$ = this.timeframe$.pipe(
    switchMap((timeframe) => {
      const { from, to } = this.buildRange(timeframe);
      return this.portfolioApi.getReturnsOverview({ from, to });
    }),
    shareReplay(1)
  );

  readonly returnsPlot$ = combineLatest([this.timeframe$, this.themeService.theme$]).pipe(
    switchMap(([timeframe, theme]) => {
      const { from, to, interval } = this.buildRange(timeframe);
      return this.portfolioApi
        .getReturnsSeries({ from, to, interval })
        .pipe(map((series) => this.toReturnsPlot(series, theme)));
    })
  );

  constructor(
    private readonly portfolioApi: PortfolioApiService,
    private readonly themeService: ThemeService
  ) {}

  onSelectTimeframe(value: Timeframe): void {
    if (this.selectedTimeframe() === value) {
      return;
    }
    this.selectedTimeframe.set(value);
    this.timeframe$.next(value);
  }

  formatPercent(value: number | null | undefined, digits: string = '1.2-2'): string {
    if (value === null || value === undefined || Number.isNaN(value)) {
      return '—';
    }
    const [_, decimalPart] = digits.split('.');
    const [minPart, maxPart] = (decimalPart ?? '2-2').split('-');
    const minimumFractionDigits = parseInt(minPart ?? '2', 10);
    const maximumFractionDigits = parseInt(maxPart ?? minPart ?? '2', 10);
    const key = `${minimumFractionDigits}-${maximumFractionDigits}`;
    let formatter = this.percentFormatters.get(key);
    if (!formatter) {
      formatter = new Intl.NumberFormat(undefined, {
        style: 'percent',
        minimumFractionDigits,
        maximumFractionDigits
      });
      this.percentFormatters.set(key, formatter);
    }
    return formatter.format(value);
  }

  trackByPosition(_: number, item: ReturnsOverviewResponse['positions'][number]): string {
    return `${item.account_id}-${item.symbol}`;
  }

  private buildRange(timeframe: Timeframe): {
    from: string;
    to: string;
    interval: string;
  } {
    const now = new Date();
    const to = now.toISOString();
    let fromDate: Date;
    switch (timeframe) {
      case '1M':
        fromDate = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
        break;
      case '3M':
        fromDate = new Date(now.getTime() - 90 * 24 * 60 * 60 * 1000);
        break;
      case 'YTD':
        fromDate = new Date(now.getFullYear(), 0, 1);
        break;
      case '1Y':
      default:
        fromDate = new Date(now.getTime() - 365 * 24 * 60 * 60 * 1000);
        break;
    }

    const spanDays = (now.getTime() - fromDate.getTime()) / (24 * 60 * 60 * 1000);
    const interval = spanDays <= 45 ? '1h' : '1d';

    return {
      from: fromDate.toISOString(),
      to,
      interval
    };
  }

  private toReturnsPlot(series: ReturnsResponse, theme: Theme): PlotDefinition | null {
    const sorted = [...series.points].sort(
      (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    );
    if (!sorted.length) {
      return null;
    }

    let cumulative = 1;
    let hasReturns = false;
    const xValues: string[] = [];
    const yValues: number[] = [];
    let baselineAdded = false;

    for (const point of sorted) {
      if (!baselineAdded) {
        xValues.push(point.timestamp);
        yValues.push(0);
        baselineAdded = true;
      }
      if (typeof point.return_pct !== 'number') {
        continue;
      }
      cumulative *= 1 + point.return_pct;
      hasReturns = true;
      xValues.push(point.timestamp);
      yValues.push((cumulative - 1) * 100);
    }

    if (!hasReturns) {
      return null;
    }

    const colors = this.themeService.getColors(theme);

    return {
      data: [
        {
          type: 'scatter',
          mode: 'lines',
          x: xValues,
          y: yValues,
          line: { color: colors.percentLine, width: 2 },
          hovertemplate: '%{x}<br>%{y:.2f}%<extra></extra>'
        }
      ],
      layout: {
        margin: { l: 55, r: 20, t: 10, b: 40 },
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        autosize: true,
        height: 320,
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
  }
}

interface PlotDefinition {
  data: any[];
  layout: any;
  config: any;
}

type Timeframe = '1M' | '3M' | 'YTD' | '1Y';

interface TimeframeOption {
  label: string;
  value: Timeframe;
}
