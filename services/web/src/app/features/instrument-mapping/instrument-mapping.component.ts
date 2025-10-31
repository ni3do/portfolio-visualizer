import { CommonModule } from '@angular/common';
import { Component, computed, inject, signal } from '@angular/core';
import { FormControl, ReactiveFormsModule } from '@angular/forms';
import { forkJoin } from 'rxjs';

import { MappedInstrument, UnmappedInstrument, YFinanceSearchResult } from '../../api/models';
import { PortfolioApiService } from '../../api/portfolio-api.service';

const hasMapping = (
  instrument: UnmappedInstrument | MappedInstrument
): instrument is MappedInstrument => 'yfinance_symbol' in instrument;

interface InstrumentMappingRow {
  instrument: UnmappedInstrument | MappedInstrument;
  currentMapping: string | null;
  shares: number | null;
  lastPrice: number | null;
  lastPriceAsOf: string | null;
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
  readonly unmappedCount = computed(
    () => this.rows().filter((row) => !row.currentMapping).length
  );

  constructor() {
    this.fetchData();
  }

  trackByInstrument = (_: number, row: InstrumentMappingRow): number =>
    row.instrument.instrument_id;

  refresh(): void {
    this.fetchData();
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

    const normalizedTarget = target.toUpperCase();
    const normalizedCurrent = row.currentMapping?.toUpperCase() ?? null;

    if (normalizedTarget === normalizedCurrent) {
      this.updateRow(instrumentId, (current) => ({
        ...current,
        error: 'This instrument is already mapped to that ticker.',
        suggestions: current.suggestions
      }));
      return;
    }

    const payload = normalizedTarget;

    row.control.setValue(payload, { emitEvent: false });

    this.updateRow(instrumentId, (current) => ({
      ...current,
      saving: true,
      error: undefined,
      suggestions: []
    }));

    this.api.updateInstrumentMapping(instrumentId, payload).subscribe({
      next: () => {
        this.fetchData();
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

  onClear(instrumentId: number): void {
    const row = this.findRow(instrumentId);
    if (!row || row.currentMapping === null) {
      return;
    }

    row.control.setValue('', { emitEvent: false });

    this.updateRow(instrumentId, (current) => ({
      ...current,
      saving: true,
      error: undefined,
      suggestions: []
    }));

    this.api.updateInstrumentMapping(instrumentId, null).subscribe({
      next: () => {
        this.fetchData();
      },
      error: () => {
        this.updateRow(instrumentId, (current) => ({
          ...current,
          saving: false,
          error: 'Failed to clear mapping. Please try again.'
        }));
      }
    });
  }

  private fetchData(): void {
    this.isLoading.set(true);
    this.loadError.set(null);
    this.rows.set([]);

    forkJoin({
      unmapped: this.api.getUnmappedInstruments(),
      mapped: this.api.getMappedInstruments()
    }).subscribe({
      next: ({ unmapped, mapped }) => {
        const merged = new Map<number, MappedInstrument | UnmappedInstrument>();

        for (const instrument of mapped.instruments) {
          merged.set(instrument.instrument_id, instrument);
        }
        for (const instrument of unmapped.instruments) {
          if (!merged.has(instrument.instrument_id)) {
            merged.set(instrument.instrument_id, instrument);
          }
        }

        const rows = Array.from(merged.values()).map((instrument) => this.createRow(instrument));

        rows.sort((a, b) => {
          const aHasMapping = a.currentMapping ? 1 : 0;
          const bHasMapping = b.currentMapping ? 1 : 0;
          if (aHasMapping !== bHasMapping) {
            return aHasMapping - bHasMapping;
          }
          const aShares = a.shares ?? 0;
          const bShares = b.shares ?? 0;
          return Number(bShares) - Number(aShares);
        });

        this.rows.set(rows);
        this.isLoading.set(false);
      },
      error: () => {
        this.loadError.set('Failed to load instrument mappings.');
        this.isLoading.set(false);
      }
    });
  }

  private createRow(instrument: UnmappedInstrument | MappedInstrument): InstrumentMappingRow {
    const sharesValue = instrument.shares ?? null;
    const mapped = hasMapping(instrument);
    const currentMapping = mapped ? instrument.yfinance_symbol ?? null : null;
    const lastPrice = mapped && instrument.last_price !== undefined ? Number(instrument.last_price) : null;
    const lastPriceAsOf = mapped ? instrument.last_price_as_of ?? null : null;
    return {
      instrument,
      currentMapping,
      shares: sharesValue !== null ? Number(sharesValue) : null,
      lastPrice,
      lastPriceAsOf,
      control: new FormControl(currentMapping ?? '', { nonNullable: true }),
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
    rows[index] = { ...current, ...updated, control: current.control };
    this.rows.set([...rows]);
  }
}
