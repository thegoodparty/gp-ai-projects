import { CampaignRecord } from './types';

interface Distribution {
  [key: string]: number;
}

interface TopItem {
  value: string;
  count: number;
  percentage: number;
}

interface StanceBreakdown {
  count: number;
  percentage: number;
}

interface SecondaryIssue {
  count: number;
  percentage: number;
  stance_breakdown: {
    [stance: string]: StanceBreakdown;
  };
}

interface HierarchicalIssue {
  count: number;
  percentage: number;
  secondary_issues: {
    [secondary: string]: SecondaryIssue;
  };
}

interface ClusterSummary {
  cluster_id: number;
  theme: string;
  category: string;
  count: number;
  percentage: number;
  sentiment: string;
  top_topics: string[];
  verbatim_quotes: string[];
}

interface DemographicGroup {
  total: number;
  sentiment?: Distribution;
  top_issues?: TopItem[];
  top_clusters?: ClusterSummary[];
}

export interface ClassifyAnalytics {
  campaign_id: string;
  record_type: 'classify';
  total_records: number;
  filters_applied: Record<string, any>;

  overall_distribution: {
    sentiment: Distribution;
    message_quality: Distribution;
    content_type: Distribution;
  };

  top_issues: TopItem[];

  hierarchical_issues: {
    [primary: string]: HierarchicalIssue;
  };

  demographic_breakdown?: {
    [key: string]: {
      [value: string]: DemographicGroup;
    };
  };
}

export interface DiscoverAnalytics {
  campaign_id: string;
  record_type: 'discover';
  total_records: number;
  filters_applied: Record<string, any>;

  cluster_analysis: {
    [clusterCount: string]: {
      total_records: number;
      clusters: ClusterSummary[];
    };
  };

  demographic_breakdown?: {
    [key: string]: {
      [value: string]: DemographicGroup;
    };
  };
}

export type AnalyticsResponse = ClassifyAnalytics | DiscoverAnalytics;

export class AnalyticsService {

  static analyzeClassifications(
    records: CampaignRecord[],
    campaignId: string,
    filtersApplied: Record<string, any>,
    breakdownBy?: string
  ): ClassifyAnalytics {

    const sentimentDist: Distribution = {};
    const qualityDist: Distribution = {};
    const contentTypeDist: Distribution = {};
    const issuePhones: Map<string, Set<string>> = new Map();

    interface HierarchicalData {
      phones: Set<string>;
      secondaries: Map<string, {
        phones: Set<string>;
        phonesAssignedToStance: Set<string>;
        stances: Map<string, Set<string>>;
      }>;
    }
    const hierarchicalData: Map<string, HierarchicalData> = new Map();

    records.forEach(record => {
      if (record.overall_sentiment) {
        sentimentDist[record.overall_sentiment] = (sentimentDist[record.overall_sentiment] || 0) + 1;
      }

      if (record.message_quality) {
        qualityDist[record.message_quality] = (qualityDist[record.message_quality] || 0) + 1;
      }

      if (record.content_type) {
        contentTypeDist[record.content_type] = (contentTypeDist[record.content_type] || 0) + 1;
      }

      if (record.hierarchical_issues && Array.isArray(record.hierarchical_issues)) {
        record.hierarchical_issues.forEach((issue: any) => {
          if (issue.primary_category) {
            if (!issuePhones.has(issue.primary_category)) {
              issuePhones.set(issue.primary_category, new Set());
            }
            issuePhones.get(issue.primary_category)!.add(record.phone_number);

            if (!hierarchicalData.has(issue.primary_category)) {
              hierarchicalData.set(issue.primary_category, {
                phones: new Set(),
                secondaries: new Map()
              });
            }
            const primaryData = hierarchicalData.get(issue.primary_category)!;
            primaryData.phones.add(record.phone_number);

            if (issue.secondary_category) {
              if (!primaryData.secondaries.has(issue.secondary_category)) {
                primaryData.secondaries.set(issue.secondary_category, {
                  phones: new Set(),
                  phonesAssignedToStance: new Set(),
                  stances: new Map()
                });
              }
              const secondaryData = primaryData.secondaries.get(issue.secondary_category)!;
              secondaryData.phones.add(record.phone_number);

              if (issue.stance && !secondaryData.phonesAssignedToStance.has(record.phone_number)) {
                if (!secondaryData.stances.has(issue.stance)) {
                  secondaryData.stances.set(issue.stance, new Set());
                }
                secondaryData.stances.get(issue.stance)!.add(record.phone_number);
                secondaryData.phonesAssignedToStance.add(record.phone_number);
              }
            }
          }
        });
      }
    });

    const topIssues = Array.from(issuePhones.entries())
      .map(([value, phones]) => ({
        value,
        count: phones.size,
        percentage: (phones.size / records.length) * 100
      }))
      .sort((a, b) => b.count - a.count);

    const hierarchicalIssues: { [primary: string]: HierarchicalIssue } = {};
    hierarchicalData.forEach((primaryData, primaryKey) => {
      const secondaryIssues: { [secondary: string]: SecondaryIssue } = {};

      primaryData.secondaries.forEach((secondaryData, secondaryKey) => {
        const stanceBreakdown: { [stance: string]: StanceBreakdown } = {};

        secondaryData.stances.forEach((stancePhones, stanceKey) => {
          stanceBreakdown[stanceKey] = {
            count: stancePhones.size,
            percentage: (stancePhones.size / secondaryData.phones.size) * 100
          };
        });

        secondaryIssues[secondaryKey] = {
          count: secondaryData.phones.size,
          percentage: (secondaryData.phones.size / records.length) * 100,
          stance_breakdown: stanceBreakdown
        };
      });

      hierarchicalIssues[primaryKey] = {
        count: primaryData.phones.size,
        percentage: (primaryData.phones.size / records.length) * 100,
        secondary_issues: secondaryIssues
      };
    });

    const analytics: ClassifyAnalytics = {
      campaign_id: campaignId,
      record_type: 'classify',
      total_records: records.length,
      filters_applied: filtersApplied,
      overall_distribution: {
        sentiment: sentimentDist,
        message_quality: qualityDist,
        content_type: contentTypeDist
      },
      top_issues: topIssues,
      hierarchical_issues: hierarchicalIssues
    };

    if (breakdownBy) {
      analytics.demographic_breakdown = this.createDemographicBreakdown(
        records,
        breakdownBy,
        'classify'
      );
    }

    return analytics;
  }

  static analyzeDiscovery(
    records: CampaignRecord[],
    campaignId: string,
    filtersApplied: Record<string, any>,
    breakdownBy?: string
  ): DiscoverAnalytics {

    const clusterCounts = ['10', '15', '20', '25'];
    const clusterAnalysis: DiscoverAnalytics['cluster_analysis'] = {};

    clusterCounts.forEach(count => {
      const clusterMap: Map<number, {
        theme: string;
        category: string;
        sentiment: string;
        topics: Set<string>;
        phones: Set<string>;
        quotes: Set<string>;
      }> = new Map();

      records.forEach(record => {
        const clusterId = record[`cluster_${count}_id`];
        const theme = record[`cluster_${count}_theme`];
        const category = record[`cluster_${count}_category`];
        const sentiment = record[`cluster_${count}_sentiment`];
        const topics = record[`cluster_${count}_topics`] || [];
        const quotes = record[`cluster_${count}_quotes`];

        if (clusterId !== undefined && clusterId !== -1) {
          if (!clusterMap.has(clusterId)) {
            clusterMap.set(clusterId, {
              theme: theme || 'Unknown',
              category: category || 'Other',
              sentiment: sentiment || 'neutral',
              topics: new Set(),
              phones: new Set(),
              quotes: new Set()
            });
          }

          const cluster = clusterMap.get(clusterId)!;
          cluster.phones.add(record.phone_number);

          if (Array.isArray(topics)) {
            topics.forEach(topic => {
              if (topic && typeof topic === 'string') {
                cluster.topics.add(topic.trim());
              }
            });
          }

          if (Array.isArray(quotes)) {
            quotes.forEach(quote => {
              if (quote && typeof quote === 'string') {
                cluster.quotes.add(quote.trim());
              }
            });
          }
        }
      });

      const clusters: ClusterSummary[] = Array.from(clusterMap.entries())
        .map(([cluster_id, data]) => ({
          cluster_id,
          theme: data.theme,
          category: data.category,
          count: data.phones.size,
          percentage: (data.phones.size / records.length) * 100,
          sentiment: data.sentiment,
          top_topics: Array.from(data.topics).slice(0, 5),
          verbatim_quotes: Array.from(data.quotes).slice(0, 3)
        }))
        .sort((a, b) => b.count - a.count);

      clusterAnalysis[`cluster_${count}`] = {
        total_records: records.length,
        clusters
      };
    });

    const analytics: DiscoverAnalytics = {
      campaign_id: campaignId,
      record_type: 'discover',
      total_records: records.length,
      filters_applied: filtersApplied,
      cluster_analysis: clusterAnalysis
    };

    if (breakdownBy) {
      analytics.demographic_breakdown = this.createDemographicBreakdown(
        records,
        breakdownBy,
        'discover'
      );
    }

    return analytics;
  }

  private static createDemographicBreakdown(
    records: CampaignRecord[],
    field: string,
    recordType: 'classify' | 'discover'
  ): Record<string, Record<string, DemographicGroup>> {

    const groups: Record<string, CampaignRecord[]> = {};

    records.forEach(record => {
      const value = String(record[field] || 'Unknown');
      if (!groups[value]) {
        groups[value] = [];
      }
      groups[value].push(record);
    });

    const breakdown: Record<string, Record<string, DemographicGroup>> = {
      [`by_${field}`]: {}
    };

    Object.entries(groups).forEach(([value, groupRecords]) => {
      const group: DemographicGroup = {
        total: groupRecords.length
      };

      if (recordType === 'classify') {
        const sentimentDist: Distribution = {};
        const issuePhones: Map<string, Set<string>> = new Map();

        groupRecords.forEach(record => {
          if (record.overall_sentiment) {
            sentimentDist[record.overall_sentiment] = (sentimentDist[record.overall_sentiment] || 0) + 1;
          }

          if (record.hierarchical_issues && Array.isArray(record.hierarchical_issues)) {
            record.hierarchical_issues.forEach((issue: any) => {
              if (issue.primary_category) {
                if (!issuePhones.has(issue.primary_category)) {
                  issuePhones.set(issue.primary_category, new Set());
                }
                issuePhones.get(issue.primary_category)!.add(record.phone_number);
              }
            });
          }
        });

        group.sentiment = sentimentDist;
        group.top_issues = Array.from(issuePhones.entries())
          .map(([val, phones]) => ({
            value: val,
            count: phones.size,
            percentage: (phones.size / groupRecords.length) * 100
          }))
          .sort((a, b) => b.count - a.count)
          .slice(0, 5);
      } else {
        const clusterCount = '10';
        const clusterMap: Map<number, { theme: string; phones: Set<string> }> = new Map();

        groupRecords.forEach(record => {
          const clusterId = record[`cluster_${clusterCount}_id`];
          const theme = record[`cluster_${clusterCount}_theme`];

          if (clusterId !== undefined && clusterId !== -1) {
            if (!clusterMap.has(clusterId)) {
              clusterMap.set(clusterId, { theme: theme || 'Unknown', phones: new Set() });
            }
            clusterMap.get(clusterId)!.phones.add(record.phone_number);
          }
        });

        group.top_clusters = Array.from(clusterMap.entries())
          .map(([cluster_id, data]) => ({
            cluster_id,
            theme: data.theme,
            category: '',
            count: data.phones.size,
            percentage: (data.phones.size / groupRecords.length) * 100,
            sentiment: '',
            top_topics: [],
            verbatim_quotes: []
          }))
          .sort((a, b) => b.count - a.count)
          .slice(0, 5);
      }

      breakdown[`by_${field}`][value] = group;
    });

    return breakdown;
  }
}