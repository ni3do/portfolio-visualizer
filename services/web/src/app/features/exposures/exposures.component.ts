import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { PlotlyModule } from 'angular-plotly.js';
import * as PlotlyJS from 'plotly.js-dist-min';
import { Observable, combineLatest, map, shareReplay } from 'rxjs';

import { PortfolioApiService } from '../../api/portfolio-api.service';
import { ExposureResponse, PortfolioExposureResponse } from '../../api/models';
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

  private readonly sectionsConfig: ReadonlyArray<{ title: string; key: Dimension }> = [
    { title: 'By Country', key: 'country' },
    { title: 'By Region', key: 'region' },
    { title: 'By Sector', key: 'sector' },
    { title: 'By Industry', key: 'industry' },
    { title: 'By Currency', key: 'currency' }
  ];

  readonly sections$: Observable<ExposureSectionView[]> = combineLatest([
    this.themeService.theme$,
    this.portfolioApi.getExposureSnapshot()
  ]).pipe(
    map(([theme, exposures]) => this.toSections(exposures, theme)),
    shareReplay(1)
  );

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

  private toSections(
    exposures: PortfolioExposureResponse,
    theme: Theme
  ): ExposureSectionView[] {
    return this.sectionsConfig.map(({ title, key }) => ({
      title,
      key,
      view: this.toView(exposures[key], theme, key)
    }));
  }

  private toView(response: ExposureResponse, theme: Theme, dimension: Dimension): ExposureView {
    const colors = this.themeService.getColors(theme);
    const labels = response.slices.map((slice) => slice.label || 'Unassigned');
    const values = response.slices.map((slice) => Math.abs(slice.value_eur));
    const totalAbs = values.reduce((acc, value) => acc + value, 0);

    const hasValues = totalAbs > 1e-6;

    const palette = colors.piePalette;
    const markerColors = labels.map((_, index) => palette[index % palette.length]);

    const plot: PlotDefinition | null = hasValues
      ? {
          data: [
            {
              type: 'pie',
              labels,
              values,
              hovertemplate:
                '%{label}<br>%{value:.2f} ' +
                this.baseCurrency +
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
        }
      : null;

    const rows = response.slices
      .map((slice) => ({
        label: slice.label || 'Unassigned',
        value: Math.abs(slice.value_eur),
        percent: slice.weight
      }))
      .filter((slice) => slice.value > 0)
      .slice(0, 12);

    return {
      plot,
      rows,
      total: totalAbs
    };
  }
}

type Dimension = keyof PortfolioExposureResponse;

function dimensionToHole(dimension: Dimension): number {
  return dimension === 'currency' ? 0.35 : 0.0;
}

interface ExposureView {
  plot: PlotDefinition | null;
  rows: Array<{ label: string; value: number; percent: number }>;
  total: number;
}

interface ExposureSectionView {
  title: string;
  key: Dimension;
  view: ExposureView;
}

interface PlotDefinition {
  data: any[];
  layout: any;
  config: any;
}
