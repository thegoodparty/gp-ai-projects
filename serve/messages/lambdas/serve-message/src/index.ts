import { APIGatewayProxyEvent, APIGatewayProxyResult, Context } from 'aws-lambda';
import { v4 as uuidv4 } from 'uuid';
import { CampaignRecord, FilterParams, RetrieveResponse, ErrorResponse, CampaignData, DynamoDBItem, SuccessResponse, AnalyticsFilterParams } from './types';
import { CampaignDataFilter } from './filters';
import { AnalyticsService, AnalyticsResponse } from './analytics';

const AWS = require('aws-sdk');
const dynamodb = new AWS.DynamoDB.DocumentClient();
const TABLE_NAME = process.env.TABLE_NAME!;

const createHeaders = (): Record<string, string> => ({
  'Content-Type': 'application/json',
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, x-api-key, Authorization',
  'Cache-Control': 'no-cache, no-store, must-revalidate',
  'Pragma': 'no-cache',
  'Expires': '0',
});

const createSuccessResponse = (data: RetrieveResponse): APIGatewayProxyResult => ({
  statusCode: 200,
  headers: createHeaders(),
  body: JSON.stringify(data),
});

const createErrorResponse = (statusCode: number, error: string, message?: string): APIGatewayProxyResult => ({
  statusCode,
  headers: createHeaders(),
  body: JSON.stringify({ error, ...(message && { message }) } as ErrorResponse),
});

const extractCampaignId = (event: APIGatewayProxyEvent): string | null => {
  // Try path parameters first (API Gateway)
  if (event.pathParameters?.campaign_id) {
    return event.pathParameters.campaign_id;
  }

  // For ALB, parse from event.path (e.g., /serve/messages/test-campaign)
  if (event.path) {
    const pathMatch = event.path.match(/\/serve\/messages\/([^/?]+)/);
    if (pathMatch && pathMatch[1]) {
      return pathMatch[1];
    }
  }

  // For Function URLs, parse from rawPath (e.g., /serve/messages/test-campaign)
  if ((event as any).rawPath) {
    const pathMatch = (event as any).rawPath.match(/\/serve\/messages\/([^/?]+)/);
    if (pathMatch && pathMatch[1]) {
      return pathMatch[1];
    }
  }

  // Fallback to direct property (direct invocation)
  if ((event as any).campaign_id) {
    return (event as any).campaign_id;
  }

  return null;
};

const extractFilters = (event: APIGatewayProxyEvent): FilterParams => {
  return event.queryStringParameters || {};
};

const queryDynamoDB = async (campaignId: string): Promise<CampaignRecord[]> => {
  console.log(`Querying DynamoDB for campaign_id: ${campaignId}`);

  const result = await dynamodb.query({
    TableName: TABLE_NAME,
    KeyConditionExpression: 'campaign_id = :cid',
    ExpressionAttributeValues: {
      ':cid': campaignId,
    },
  }).promise();

  return (result.Items || []) as CampaignRecord[];
};

// POST-specific functions
const parseRequestBody = (event: APIGatewayProxyEvent): CampaignData => {
  try {
    if (!event.body) {
      throw new Error('Request body is required');
    }
    return JSON.parse(event.body) as CampaignData;
  } catch (error) {
    throw new Error('Invalid JSON in request body');
  }
};

const validateRequired = (data: CampaignData): string | null => {
  if (!data.campaign_id) {
    return 'campaign_id is required';
  }
  return null;
};

const createDynamoDBItem = (data: CampaignData): DynamoDBItem => {
  const timestamp = new Date().toISOString();
  const recordId = data.record_id || uuidv4();

  return {
    ...Object.fromEntries(
      Object.entries(data).filter(([key]) =>
        !['record_id', 'created_at', 'updated_at'].includes(key)
      )
    ),
    campaign_id: data.campaign_id,
    record_id: recordId,
    updated_at: timestamp,
    created_at: data.created_at || timestamp,
  };
};


export const handler = async (
  event: APIGatewayProxyEvent,
  context: Context
): Promise<APIGatewayProxyResult> => {
  console.log('Event:', JSON.stringify(event, null, 2));
  console.log('Context:', JSON.stringify(context, null, 2));

  try {
    const httpMethod = event.httpMethod || (event as any).requestContext?.http?.method || 'GET';
    console.log(`HTTP Method: ${httpMethod}`);

    if (httpMethod === 'POST') {
      return await handlePost(event);
    } else if (httpMethod === 'GET') {
      return await handleGet(event);
    } else {
      return createErrorResponse(405, 'Method Not Allowed', `HTTP method ${httpMethod} is not supported`);
    }

  } catch (error) {
    console.error('Error:', error);

    const errorMessage = error instanceof Error ? error.message : 'Unknown error occurred';

    return createErrorResponse(
      500,
      'Internal server error',
      errorMessage
    );
  }
};

const handleGet = async (event: APIGatewayProxyEvent): Promise<APIGatewayProxyResult> => {
  const campaignId = extractCampaignId(event);
  if (!campaignId) {
    return createErrorResponse(400, 'campaign_id is required');
  }

  const path = event.path || (event as any).rawPath || '';

  if (path.includes('/analytics')) {
    return await handleAnalytics(event, campaignId);
  }

  const filterParams = extractFilters(event);
  console.log('Filters:', filterParams);

  const allRecords = await queryDynamoDB(campaignId);
  console.log(`Found ${allRecords.length} total records`);

  const filter = new CampaignDataFilter(filterParams);
  let filteredRecords = filter.applyFilters(allRecords);
  console.log(`After filtering: ${filteredRecords.length} records`);

  filteredRecords = filter.applySorting(filteredRecords);

  const { paginatedRecords, hasMore } = filter.applyPagination(filteredRecords);
  console.log(`After pagination: ${paginatedRecords.length} records (showing)`);

  const limit = filterParams.limit ? parseInt(filterParams.limit) : null;
  const offset = filterParams.offset ? parseInt(filterParams.offset) : 0;

  const response: RetrieveResponse = {
    campaign_id: campaignId,
    total_records: allRecords.length,
    filtered_records: filteredRecords.length,
    returned_records: paginatedRecords.length,
    filters_applied: filter.getAppliedFilters(),
    pagination: {
      limit: !isNaN(limit!) ? limit : null,
      offset: !isNaN(offset) ? offset : 0,
      has_more: hasMore,
    },
    data: paginatedRecords,
  };

  return createSuccessResponse(response);
};

const handleAnalytics = async (event: APIGatewayProxyEvent, campaignId: string): Promise<APIGatewayProxyResult> => {
  const filterParams = extractFilters(event) as AnalyticsFilterParams;
  console.log('Analytics filters:', filterParams);

  if (!filterParams.type || (filterParams.type !== 'classify' && filterParams.type !== 'discover')) {
    return createErrorResponse(400, 'type parameter is required and must be "classify" or "discover"');
  }

  const finalCampaignId = filterParams.campaign_id || campaignId;
  if (!finalCampaignId || finalCampaignId === 'analytics') {
    return createErrorResponse(400, 'campaign_id is required (in path or query parameter)');
  }

  const allRecords = await queryDynamoDB(finalCampaignId);
  console.log(`Found ${allRecords.length} total records for analytics`);

  const filter = new CampaignDataFilter(filterParams);
  const filteredRecords = filter.applyFilters(allRecords);
  console.log(`After filtering: ${filteredRecords.length} records`);

  let analytics: AnalyticsResponse;

  if (filterParams.type === 'classify') {
    analytics = AnalyticsService.analyzeClassifications(
      filteredRecords,
      finalCampaignId,
      filter.getAppliedFilters(),
      filterParams.breakdown_by
    );
  } else {
    analytics = AnalyticsService.analyzeDiscovery(
      filteredRecords,
      finalCampaignId,
      filter.getAppliedFilters(),
      filterParams.breakdown_by
    );
  }

  return {
    statusCode: 200,
    headers: createHeaders(),
    body: JSON.stringify(analytics),
  };
};

const handlePost = async (event: APIGatewayProxyEvent): Promise<APIGatewayProxyResult> => {
  const requestData = parseRequestBody(event);

  // Validate required fields
  const validationError = validateRequired(requestData);
  if (validationError) {
    return createErrorResponse(400, validationError);
  }

  // Create the DynamoDB item
  const item = createDynamoDBItem(requestData);

  console.log('Putting item:', JSON.stringify(item, null, 2));

  // Write to DynamoDB
  await dynamodb.put({
    TableName: TABLE_NAME,
    Item: item,
  }).promise();

  console.log('Successfully created/updated record');

  const successResponse: SuccessResponse = {
    success: true,
    item,
  };

  return {
    statusCode: 200,
    headers: createHeaders(),
    body: JSON.stringify(successResponse),
  };
};