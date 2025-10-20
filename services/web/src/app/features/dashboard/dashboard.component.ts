import { CommonModule, AsyncPipe, DecimalPipe } from '@angular/common';
import { Component, signal } from '@angular/core';
import { NgChartsModule } from 'ng2-charts';
import { PlotlyModule } from 'angular-plotly.js';
import { BehaviorSubject, Observable, map, switchMap } from 'rxjs';
import * as PlotlyJS from 'plotly.js-dist-min';
import { ChartConfiguration, ChartOptions } from 'chart.js';

import { PortfolioApiService } from '../../api/portfolio-api.service';
import {
  ExposureResponse,
  PortfolioSeries,
  UnrealizedResponse
} from '../../api/models';

PlotlyModule.plotlyjs = PlotlyJS;

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, AsyncPipe, DecimalPipe, NgChartsModule, PlotlyModule],
  templateUrl: './dashboard.component.html',
  styleUrl: './dashboard.component.scss'
})
export class DashboardComponent {
  private readonly timeframe$ = new BehaviorSubject<Timeframe>('1M');

  readonly navSeries$: Observable<PortfolioSeries> = this.timeframe$.pipe(
    switchMap((timeframe) => this.portfolioApi.getNavSeries(this.buildNavParams(timeframe)))
  );
  readonly navData$ = this.navSeries$.pipe(
    map((series) => ({ series, plot: this.toNavPlot(series) }))
  );
  readonly countryExposure$: Observable<ExposureResponse> = this.portfolioApi.getExposure('country');
  readonly sectorExposure$: Observable<ExposureResponse> = this.portfolioApi.getExposure('sector');
  readonly currencyExposure$: Observable<ExposureResponse> = this.portfolioApi.getExposure('currency');
  readonly unrealized$: Observable<UnrealizedResponse> = this.portfolioApi.getUnrealized();

  readonly exposureChartOptions: ChartOptions<'doughnut'> = {
    responsive: true,
    plugins: {
      legend: {
        position: 'bottom',
        labels: { boxWidth: 14 }
      }
    }
  };

  constructor(private readonly portfolioApi: PortfolioApiService) {}

  readonly timeframes: TimeframeOption[] = [
    { label: '1W', value: '1W' },
    { label: '1M', value: '1M' },
    { label: 'YTD', value: 'YTD' },
    { label: '1Y', value: '1Y' },
  ];

  readonly selectedTimeframe = signal<Timeframe>('1M');

  onSelectTimeframe(value: Timeframe): void {
    if (this.selectedTimeframe() === value) {
      return;
    }
    this.selectedTimeframe.set(value);
    this.timeframe$.next(value);
  }

  toNavPlot(series: PortfolioSeries): NavPlot | null {
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
          line: { color: '#2563eb', width: 2 },
          fill: 'tozeroy',
          fillcolor: 'rgba(37, 99, 235, 0.15)',
          hovertemplate: '%{x}<br>%{y:.2f} EUR<extra></extra>',
        },
      ],
      layout: {
        margin: { l: 55, r: 20, t: 10, b: 40 },
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        autosize: true,
        height: 280,
        xaxis: {
          type: 'date',
          tickfont: { color: '#475569' },
          gridcolor: 'rgba(71, 85, 105, 0.15)',
        },
        yaxis: {
          tickfont: { color: '#475569' },
          gridcolor: 'rgba(71, 85, 105, 0.15)',
          separatethousands: true,
        },
      },
      config: {
        responsive: true,
        displayModeBar: false,
      },
    };
  }

  toExposureChart(exposure: ExposureResponse): ChartConfiguration<'doughnut'>['data'] {
    const labels = exposure.slices.map((slice) => slice.label || 'Unassigned');
    const data = exposure.slices.map((slice) => slice.value_eur);
    return {
      labels,
      datasets: [
        {
          data,
          backgroundColor: [
            '#1d4ed8',
            '#2563eb',
            '#3b82f6',
            '#60a5fa',
            '#93c5fd',
            '#bfdbfe'
          ]
        }
      ]
    };
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
      interval,
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
