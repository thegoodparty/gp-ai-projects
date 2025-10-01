export interface CampaignRecord {
  campaign_id: string;
  record_id: string;
  record_type?: 'message' | 'classify' | 'discover';
  phone_number: string;

  // Message data (record_type='message')
  message_text?: string;
  sent_at?: string;
  round?: string;
  carrier?: string;
  campaign_name?: string;

  // Demographics (all record types)
  age?: number;
  age_group?: string;
  location?: string;
  ward?: string;
  voters_gender?: string;
  voting_performance_category?: string;
  residence_city?: string;
  homeowner_status?: string;
  business_owner?: string;
  has_children_under_18?: string;
  education_level?: string;
  income_level?: string;

  // Classification data (record_type='classify')
  primary_issue_category?: string;
  secondary_issue?: string;
  issue_stance?: string;
  overall_sentiment?: string;
  message_quality?: string;
  content_type?: string;
  classification_confidence?: number;
  is_substantive?: boolean;
  hierarchical_issues?: any[];

  // Discovery/clustering data (record_type='discover')
  cluster_10_id?: number;
  cluster_10_theme?: string;
  cluster_10_category?: string;
  cluster_10_topics?: string[];
  cluster_10_sentiment?: string;
  cluster_10_relevance?: string;
  cluster_10_confidence?: number;
  cluster_10_analysis?: string;
  cluster_10_quotes?: string;

  cluster_15_id?: number;
  cluster_15_theme?: string;
  cluster_15_category?: string;
  cluster_15_topics?: string[];
  cluster_15_sentiment?: string;
  cluster_15_relevance?: string;
  cluster_15_confidence?: number;
  cluster_15_analysis?: string;
  cluster_15_quotes?: string;

  cluster_20_id?: number;
  cluster_20_theme?: string;
  cluster_20_category?: string;
  cluster_20_topics?: string[];
  cluster_20_sentiment?: string;
  cluster_20_relevance?: string;
  cluster_20_confidence?: number;
  cluster_20_analysis?: string;
  cluster_20_quotes?: string;

  cluster_25_id?: number;
  cluster_25_theme?: string;
  cluster_25_category?: string;
  cluster_25_topics?: string[];
  cluster_25_sentiment?: string;
  cluster_25_relevance?: string;
  cluster_25_confidence?: number;
  cluster_25_analysis?: string;
  cluster_25_quotes?: string;

  created_at: string;
  updated_at: string;
  [key: string]: any; // Allow additional fields
}

export interface FilterParams {
  // Record type filter
  type?: 'message' | 'classify' | 'discover' | 'all';

  // Age filters
  age?: string;
  age_min?: string;
  age_max?: string;

  // Location filter
  location?: string;

  // Income filters
  income?: string;
  income_min?: string;
  income_max?: string;

  // Boolean filters
  homeowner?: string;
  business_owner?: string;
  families_with_children?: string;

  // Education filter
  education_level?: string;

  // Demographic filters
  age_group?: string;
  overall_sentiment?: string;
  voting_performance_category?: string;
  voters_gender?: string;
  ward?: string;
  residence_city?: string;

  // Sorting and pagination
  sort_by?: string;
  sort_order?: 'asc' | 'desc';
  limit?: string;
  offset?: string;
}

export interface AppliedFilters {
  record_type?: string;
  age?: number;
  age_range?: { min: number; max: number };
  location?: string;
  income?: number;
  income_range?: { min: number; max: number };
  homeowner?: boolean;
  business_owner?: boolean;
  families_with_children?: boolean;
  education_level?: string;
  age_group?: string;
  overall_sentiment?: string;
  voting_performance_category?: string;
  voters_gender?: string;
  ward?: string;
  residence_city?: string;
  sort_by?: string;
  sort_order?: string;
  [key: string]: any;
}

export interface PaginationInfo {
  limit: number | null;
  offset: number;
  has_more: boolean;
}

export interface RetrieveResponse {
  campaign_id: string;
  total_records: number;
  filtered_records: number;
  returned_records: number;
  filters_applied: AppliedFilters;
  pagination: PaginationInfo;
  data: CampaignRecord[];
}

export interface ErrorResponse {
  error: string;
  message?: string;
}

// POST-specific types
export interface CampaignData {
  campaign_id: string;
  record_id?: string;
  age?: number;
  location?: string;
  income?: number;
  homeowner?: boolean;
  business_owner?: boolean;
  families_with_children?: boolean;
  education_level?: string;
  data?: Record<string, any>;
  created_at?: string;
  [key: string]: any; // Allow additional fields
}

export interface DynamoDBItem extends CampaignData {
  record_id: string;
  created_at: string;
  updated_at: string;
}

export interface SuccessResponse {
  success: true;
  item: DynamoDBItem;
}

export interface AnalyticsFilterParams extends FilterParams {
  campaign_id?: string;
  breakdown_by?: string;
}