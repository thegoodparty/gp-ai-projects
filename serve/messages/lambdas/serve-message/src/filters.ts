import { CampaignRecord, FilterParams, AppliedFilters } from './types';

export class CampaignDataFilter {
  private filters: FilterParams;
  private appliedFilters: AppliedFilters = {};

  constructor(filters: FilterParams) {
    this.filters = filters;
  }

  private applyStringFilter(
    records: CampaignRecord[],
    filterKey: keyof FilterParams,
    recordKey: keyof CampaignRecord,
    exactMatch: boolean = true
  ): CampaignRecord[] {
    const filterValue = this.filters[filterKey];
    if (!filterValue || typeof filterValue !== 'string') {
      return records;
    }

    const filterLower = filterValue.toLowerCase();
    const filtered = records.filter(record => {
      const recordValue = record[recordKey];
      if (!recordValue || typeof recordValue !== 'string') {
        return false;
      }
      const recordLower = recordValue.toLowerCase();
      return exactMatch ? recordLower === filterLower : recordLower.includes(filterLower);
    });

    this.appliedFilters[filterKey as string] = filterValue;
    return filtered;
  }

  public applyFilters(records: CampaignRecord[]): CampaignRecord[] {
    let filteredRecords = [...records];

    // Type filter (message, classify, discover, or all)
    if (this.filters.type && this.filters.type !== 'all') {
      filteredRecords = filteredRecords.filter(r => {
        if (!r.record_id) return false;
        // Check if record_id starts with the type prefix
        return r.record_id.startsWith(`${this.filters.type}#`);
      });
      this.appliedFilters.record_type = this.filters.type;
    }

    // Age filter
    if (this.filters.age) {
      const ageValue = parseInt(this.filters.age);
      if (!isNaN(ageValue)) {
        filteredRecords = filteredRecords.filter(r => r.age === ageValue);
        this.appliedFilters.age = ageValue;
      }
    }

    // Age range filter
    if (this.filters.age_min || this.filters.age_max) {
      const ageMin = this.filters.age_min ? parseInt(this.filters.age_min) : 0;
      const ageMax = this.filters.age_max ? parseInt(this.filters.age_max) : 150;

      if (!isNaN(ageMin) && !isNaN(ageMax)) {
        filteredRecords = filteredRecords.filter(r =>
          r.age !== undefined && r.age >= ageMin && r.age <= ageMax
        );
        this.appliedFilters.age_range = { min: ageMin, max: ageMax };
      }
    }

    filteredRecords = this.applyStringFilter(filteredRecords, 'location', 'location', false);

    // Income filters
    if (this.filters.income) {
      const incomeValue = parseInt(this.filters.income);
      if (!isNaN(incomeValue)) {
        filteredRecords = filteredRecords.filter(r => r.income === incomeValue);
        this.appliedFilters.income = incomeValue;
      }
    }

    if (this.filters.income_min || this.filters.income_max) {
      const incomeMin = this.filters.income_min ? parseInt(this.filters.income_min) : 0;
      const incomeMax = this.filters.income_max ? parseInt(this.filters.income_max) : 999999999;

      if (!isNaN(incomeMin) && !isNaN(incomeMax)) {
        filteredRecords = filteredRecords.filter(r =>
          r.income !== undefined && r.income >= incomeMin && r.income <= incomeMax
        );
        this.appliedFilters.income_range = { min: incomeMin, max: incomeMax };
      }
    }

    // Boolean filters
    if (this.filters.homeowner !== undefined) {
      const isHomeowner = this.filters.homeowner === 'true';
      filteredRecords = filteredRecords.filter(r => r.homeowner === isHomeowner);
      this.appliedFilters.homeowner = isHomeowner;
    }

    if (this.filters.business_owner !== undefined) {
      const isBusinessOwner = this.filters.business_owner === 'true';
      filteredRecords = filteredRecords.filter(r => r.business_owner === (isBusinessOwner ? 'true' : 'false'));
      this.appliedFilters.business_owner = isBusinessOwner;
    }

    if (this.filters.families_with_children !== undefined) {
      const hasFamilies = this.filters.families_with_children === 'true';
      filteredRecords = filteredRecords.filter(r => r.families_with_children === hasFamilies);
      this.appliedFilters.families_with_children = hasFamilies;
    }

    filteredRecords = this.applyStringFilter(filteredRecords, 'education_level', 'education_level');
    filteredRecords = this.applyStringFilter(filteredRecords, 'age_group', 'age_group');
    filteredRecords = this.applyStringFilter(filteredRecords, 'overall_sentiment', 'overall_sentiment');
    filteredRecords = this.applyStringFilter(filteredRecords, 'voting_performance_category', 'voting_performance_category');
    filteredRecords = this.applyStringFilter(filteredRecords, 'voters_gender', 'voters_gender');
    filteredRecords = this.applyStringFilter(filteredRecords, 'ward', 'ward', false);
    filteredRecords = this.applyStringFilter(filteredRecords, 'residence_city', 'residence_city', false);

    return filteredRecords;
  }

  public applySorting(records: CampaignRecord[]): CampaignRecord[] {
    if (!this.filters.sort_by) {
      return records;
    }

    const sortField = this.filters.sort_by;
    const sortOrder = this.filters.sort_order === 'desc' ? -1 : 1;

    const sorted = records.sort((a, b) => {
      const aVal = a[sortField as keyof CampaignRecord];
      const bVal = b[sortField as keyof CampaignRecord];

      if (aVal === undefined && bVal === undefined) return 0;
      if (aVal === undefined) return 1;
      if (bVal === undefined) return -1;

      if (aVal < bVal) return -1 * sortOrder;
      if (aVal > bVal) return 1 * sortOrder;
      return 0;
    });

    this.appliedFilters.sort_by = sortField;
    this.appliedFilters.sort_order = this.filters.sort_order || 'asc';

    return sorted;
  }

  public applyPagination(records: CampaignRecord[]): {
    paginatedRecords: CampaignRecord[];
    hasMore: boolean;
  } {
    const limit = this.filters.limit ? parseInt(this.filters.limit) : undefined;
    const offset = this.filters.offset ? parseInt(this.filters.offset) : 0;

    let paginatedRecords = records;
    let hasMore = false;

    if (!isNaN(offset) && offset > 0) {
      paginatedRecords = paginatedRecords.slice(offset);
    }

    if (limit && !isNaN(limit) && limit > 0) {
      hasMore = paginatedRecords.length > limit;
      paginatedRecords = paginatedRecords.slice(0, limit);
    }

    return { paginatedRecords, hasMore };
  }

  public getAppliedFilters(): AppliedFilters {
    return this.appliedFilters;
  }
}