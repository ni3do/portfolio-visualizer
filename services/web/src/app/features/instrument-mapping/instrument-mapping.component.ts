import { CommonModule } from '@angular/common';
import { Component, inject, signal } from '@angular/core';
import { FormControl, ReactiveFormsModule } from '@angular/forms';

import {
  UnmappedInstrument,
  YFinanceSearchResult
} from '../../api/models';
import { PortfolioApiService } from '../../api/portfolio-api.service';

interface InstrumentMappingRow {
  instrument: UnmappedInstrument;
  control: FormControl<string>;
  suggestions: YFinanceSearchResult[];
  loading: boolean;
  saving: boolean;
  error?: string;
}

@Component({
  selector: 'app-instrument-mapping',
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule],
  templateUrl: './instrument-mapping.component.html',
  styleUrl: './instrument-mapping.component.scss'
})
export class InstrumentMappingComponent {
  private readonly api = inject(PortfolioApiService);

  readonly isLoading = signal<boolean>(false);
  readonly loadError = signal<string | null>(null);
  readonly rows = signal<InstrumentMappingRow[]>([]);

  constructor() {
    this.fetchUnmapped();
  }

  trackByInstrument = (_: number, row: InstrumentMappingRow): number =>
    row.instrument.instrument_id;

  refresh(): void {
    this.fetchUnmapped();
  }

  onSearch(instrumentId: number): void {
    const row = this.findRow(instrumentId);
    if (!row) {
      return;
    }
    const query = row.control.value.trim();
    if (!query) {
      this.updateRow(instrumentId, (current) => ({
        ...current,
        error: 'Enter a search term to look up tickers.',
        suggestions: []
      }));
      return;
    }

    this.updateRow(instrumentId, (current) => ({
      ...current,
      loading: true,
      error: undefined,
      suggestions: []
    }));

    this.api.searchYfinanceSymbols(query).subscribe({
      next: (response) => {
        this.updateRow(instrumentId, (current) => ({
          ...current,
          loading: false,
          suggestions: response.results,
          error: response.results.length ? undefined : 'No matches found for your query.'
        }));
      },
      error: () => {
        this.updateRow(instrumentId, (current) => ({
          ...current,
          loading: false,
          suggestions: [],
          error: 'Search failed. Please try again.'
        }));
      }
    });
  }

  onApply(instrumentId: number, symbol?: string): void {
    const row = this.findRow(instrumentId);
    if (!row) {
      return;
    }

    const target = (symbol ?? row.control.value).trim();
    if (!target) {
      this.updateRow(instrumentId, (current) => ({
        ...current,
        error: 'Provide a ticker symbol before mapping.',
        suggestions: current.suggestions
      }));
      return;
    }

    this.updateRow(instrumentId, (current) => ({
      ...current,
      saving: true,
      error: undefined
    }));

    this.api.updateInstrumentMapping(instrumentId, target).subscribe({
      next: () => {
        const remaining = this.rows().filter(
          (item) => item.instrument.instrument_id !== instrumentId
        );
        this.rows.set(remaining);
      },
      error: () => {
        this.updateRow(instrumentId, (current) => ({
          ...current,
          saving: false,
          error: 'Failed to update mapping. Please try again.'
        }));
      }
    });
  }

  private fetchUnmapped(): void {
    this.isLoading.set(true);
    this.loadError.set(null);
    this.rows.set([]);

    this.api.getUnmappedInstruments().subscribe({
      next: (response) => {
        const rows = response.instruments.map((instrument) => this.createRow(instrument));
        this.rows.set(rows);
        this.isLoading.set(false);
      },
      error: () => {
        this.loadError.set('Failed to load unmapped instruments.');
        this.isLoading.set(false);
      }
    });
  }

  private createRow(instrument: UnmappedInstrument): InstrumentMappingRow {
    return {
      instrument,
      control: new FormControl('', { nonNullable: true }),
      suggestions: [],
      loading: false,
      saving: false
    };
  }

  private findRow(instrumentId: number): InstrumentMappingRow | undefined {
    return this.rows().find((row) => row.instrument.instrument_id === instrumentId);
  }

  private updateRow(
    instrumentId: number,
    mutate: (row: InstrumentMappingRow) => InstrumentMappingRow
  ): void {
    const rows = this.rows();
    const index = rows.findIndex((row) => row.instrument.instrument_id === instrumentId);
    if (index === -1) {
      return;
    }
    const current = rows[index];
    const updated = mutate(current);
    rows[index] = { ...updated, control: current.control };
    this.rows.set([...rows]);
  }
}
