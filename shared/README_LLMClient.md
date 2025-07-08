# LLMClient with Provider Fallback

This enhanced `LLMClient` provides automatic fallback between multiple LLM providers, with Gemini 2.5 Flash as the primary provider and TogetherAI as fallback.

## Features

- **Automatic Fallback**: If Gemini fails, automatically tries TogetherAI
- **Multiple Providers**: Support for any number of providers with priority ordering
- **Retry Logic**: Configurable retry attempts per provider with exponential backoff
- **Token Tracking**: Comprehensive token usage statistics across all providers
- **Provider Transparency**: Know which provider was used for each request
- **Connectivity Testing**: Built-in connectivity testing for all providers

## Environment Variables

Set up your API keys in your `.env` file:

```env
# Gemini API Key (Primary provider)
# Get your key from: https://aistudio.google.com/app/apikey
GEMINI_API_KEY=your_gemini_api_key_here

# TogetherAI API Key (Fallback provider)
# Get your key from: https://api.together.xyz/settings/api-keys
TOGETHER_API_KEY=your_together_api_key_here
```

## Usage Examples

### Basic Usage (Default Configuration)

```python
from shared.llm import LLMClient

# Initialize with default Gemini + TogetherAI fallback
client = LLMClient()

# Make a request (will try Gemini first, then TogetherAI if needed)
response = client.create_completion(
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the capital of France?"}
    ]
)

print(f"Provider used: {response.provider_used}")
print(f"Model used: {response.model_used}")
print(response.choices[0].message.content)
```

### Convenience Method

```python
# Quick setup with default configuration
client = LLMClient.create_gemini_with_together_fallback()
```

### Custom Configuration

```python
custom_providers = [
    {
        "name": "gemini",
        "api_key": "your_gemini_key",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.5-flash",
        "priority": 1
    },
    {
        "name": "together",
        "api_key": "your_together_key",
        "base_url": "https://api.together.xyz/v1",
        "default_model": "deepseek-ai/DeepSeek-V3",
        "priority": 2
    }
]

client = LLMClient(
    providers=custom_providers,
    max_retries=3,
    base_delay=1.0,
    fallback_on_provider_failure=True
)
```

### Testing Provider Connectivity

```python
# Test all providers
connectivity = client.test_provider_connectivity()
for provider in connectivity['providers']:
    status = "✅ Connected" if provider['status'] == 'connected' else "❌ Failed"
    print(f"{provider['name']}: {status}")
```

### Using Specific Models

```python
# Use a specific model (will still fallback to other providers if needed)
response = client.create_completion(
    messages=[...],
    model="gemini-2.5-flash"  # Try this model first
)
```

### Structured Completions

```python
from pydantic import BaseModel
from typing import List

class CityInfo(BaseModel):
    city: str
    country: str
    population: int
    landmarks: List[str]

# Works with fallback too
result = client.create_structured_completion(
    messages=[...],
    response_schema=CityInfo
)
```

## How Fallback Works

1. **Primary Provider**: Request goes to Gemini 2.5 Flash first
2. **Retry Logic**: If Gemini fails, it retries up to `max_retries` times with exponential backoff
3. **Provider Fallback**: If all retries fail, it moves to the next provider (TogetherAI)
4. **Final Attempt**: TogetherAI gets its own retry attempts
5. **Error Handling**: If all providers fail, raises a comprehensive error

## Configuration Options

- `providers`: List of provider configurations
- `max_retries`: Maximum retry attempts per provider (default: 3)
- `base_delay`: Base delay for exponential backoff in seconds (default: 1.0)
- `fallback_on_provider_failure`: Whether to fallback to next provider (default: True)

## Token Usage Tracking

```python
# Get comprehensive token usage statistics
stats = client.get_token_usage_stats()
print(f"Total tokens: {stats['total_tokens']}")
print(f"API calls made: {stats['api_call_count']}")
print(f"Average tokens per call: {stats['average_total_tokens']}")

# Reset statistics
previous_stats = client.reset_token_usage_stats()
```

## Provider Information

```python
# Get information about configured providers
info = client.get_current_provider_info()
print(f"Total providers: {info['total_providers']}")
for provider in info['providers']:
    print(f"- {provider['name']}: {provider['default_model']}")
```

## Error Handling

The client provides detailed error information:

```python
try:
    response = client.create_completion(messages=[...])
except RuntimeError as e:
    print(f"All providers failed: {e}")
    # Error message includes details about which providers were tried
```

## Best Practices

1. **Environment Variables**: Always use environment variables for API keys
2. **Connectivity Testing**: Test connectivity during application startup
3. **Error Handling**: Always wrap LLM calls in try-catch blocks
4. **Token Monitoring**: Monitor token usage to manage costs
5. **Model Selection**: Use specific models when you have preferences
6. **Retry Configuration**: Adjust retry settings based on your use case

## Benefits

- **Reliability**: Automatic fallback increases system reliability
- **Cost Optimization**: Use cheaper providers as fallback
- **Performance**: Fast primary provider with reliable fallback
- **Transparency**: Always know which provider was used
- **Flexibility**: Easy to add/remove providers or change priorities
