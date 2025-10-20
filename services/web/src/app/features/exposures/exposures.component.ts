import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { PlotlyModule } from 'angular-plotly.js';
import * as PlotlyJS from 'plotly.js-dist-min';
import { Observable, combineLatest, map, shareReplay } from 'rxjs';

import { PortfolioApiService } from '../../api/portfolio-api.service';
import { ExposureResponse } from '../../api/models';
import { environment } from '../../../environments/environment';
import { ThemeService, Theme } from '../../core/services/theme.service';

PlotlyModule.plotlyjs = PlotlyJS;

@Component({
  selector: 'app-exposures',
  standalone: true,
  imports: [CommonModule, PlotlyModule],
  templateUrl: './exposures.component.html',
  styleUrl: './exposures.component.scss'
})
export class ExposuresComponent {
  private readonly baseCurrency = environment.baseCurrency;
  private readonly currencyFormatter = new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency: this.baseCurrency,
    maximumFractionDigits: 2
  });
  private readonly percentFormatter = new Intl.NumberFormat(undefined, {
    style: 'percent',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });

  private readonly exposureCache = new Map<Dimension, Observable<ExposureResponse>>();

  readonly sections: ExposureSection[] = [
    { title: 'By Country', stream: this.loadExposure('country') },
    { title: 'By Region', stream: this.loadExposure('region') },
    { title: 'By Sector', stream: this.loadExposure('sector') },
    { title: 'By Industry', stream: this.loadExposure('industry') },
    { title: 'By Currency', stream: this.loadExposure('currency') }
  ];

  constructor(
    private readonly portfolioApi: PortfolioApiService,
    private readonly themeService: ThemeService
  ) {}

  formatCurrency(value: number): string {
    return this.currencyFormatter.format(value);
  }

  formatPercent(value: number | null | undefined): string {
    return this.percentFormatter.format(value ?? 0);
  }

  private loadExposure(dimension: Dimension): Observable<ExposureView> {
    return combineLatest([
      this.themeService.theme$,
      this.getExposureResponse(dimension)
    ]).pipe(map(([theme, response]) => this.toView(response, theme, dimension)));
  }

  private getExposureResponse(dimension: Dimension): Observable<ExposureResponse> {
    if (!this.exposureCache.has(dimension)) {
      this.exposureCache.set(
        dimension,
        this.portfolioApi.getExposure(dimension).pipe(shareReplay(1))
      );
    }
    return this.exposureCache.get(dimension)!;
  }

  private toView(response: ExposureResponse, theme: Theme, dimension: Dimension): ExposureView {
    const colors = this.themeService.getColors(theme);
    const labels = response.slices.map((slice) => slice.label || 'Unassigned');
    const values = response.slices.map((slice) => slice.value_eur);

    const palette = colors.piePalette;
    const markerColors = labels.map((_, index) => palette[index % palette.length]);

    const plot: PlotDefinition = {
      data: [
        {
          type: 'pie',
          labels,
          values,
          hovertemplate: '%{label}<br>%{value:.2f} ' + this.baseCurrency +
            '<br>%{percent:.1%}<extra></extra>',
          textinfo: 'label+percent',
          textfont: { size: 13 },
          hole: dimensionToHole(dimension),
          marker: {
            colors: markerColors
          }
        }
      ],
      layout: {
        margin: { l: 10, r: 10, t: 10, b: 10 },
        height: 260,
        autosize: true,
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        legend: {
          orientation: 'h',
          yanchor: 'top',
          y: -0.15,
          xanchor: 'center',
          x: 0.5,
          font: { color: colors.text }
        },
        font: { color: colors.text }
      },
      config: {
        responsive: true,
        displayModeBar: false
      }
    };

    const rows = response.slices
      .map((slice) => ({
        label: slice.label || 'Unassigned',
        value: slice.value_eur,
        percent: slice.weight
      }))
      .slice(0, 12);

    return {
      plot,
      rows,
      total: response.total_eur
    };
  }
}

type Dimension = 'country' | 'region' | 'sector' | 'industry' | 'currency';

function dimensionToHole(dimension: Dimension): number {
  return dimension === 'currency' ? 0.35 : 0.0;
}

interface ExposureView {
  plot: PlotDefinition;
  rows: Array<{ label: string; value: number; percent: number }>;
  total: number;
}

interface ExposureSection {
  title: string;
  stream: Observable<ExposureView>;
}

interface PlotDefinition {
  data: any[];
  layout: any;
  config: any;
}
