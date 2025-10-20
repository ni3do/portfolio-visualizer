import { CommonModule } from '@angular/common';
import { Component } from '@angular/core';
import { AsyncPipe, DecimalPipe } from '@angular/common';
import { Observable } from 'rxjs';

import { PortfolioApiService } from '../../api/portfolio-api.service';
import { PositionsResponse } from '../../api/models';

@Component({
  selector: 'app-positions',
  standalone: true,
  imports: [CommonModule, AsyncPipe, DecimalPipe],
  templateUrl: './positions.component.html',
  styleUrl: './positions.component.scss'
})
export class PositionsComponent {
  readonly positions$: Observable<PositionsResponse> = this.portfolioApi.getPositions();

  constructor(private readonly portfolioApi: PortfolioApiService) {}
}
