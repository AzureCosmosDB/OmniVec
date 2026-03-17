package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sync"
	"sync/atomic"
	"time"
)

type EmbeddingRequest struct {
	Input []string `json:"input"`
}

type EmbeddingResponse struct {
	Data  []struct{ Embedding []float32 } `json:"data"`
	Usage struct {
		TotalTokens int `json:"total_tokens"`
	} `json:"usage"`
}

func main() {
	endpoint := os.Getenv("AZURE_OPENAI_ENDPOINT")
	apiKey := os.Getenv("AZURE_OPENAI_KEY")
	deployment := os.Getenv("AZURE_OPENAI_DEPLOYMENT")
	if deployment == "" {
		deployment = "text-embedding-3-small"
	}

	if endpoint == "" || apiKey == "" {
		fmt.Println("Usage: AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_KEY=... go run bench_embedding.go")
		os.Exit(1)
	}

	url := fmt.Sprintf("%s/openai/deployments/%s/embeddings?api-version=2024-02-01", endpoint, deployment)

	// Build batch payload (100 texts)
	batchSize := 100
	texts := make([]string, batchSize)
	for i := 0; i < batchSize; i++ {
		texts[i] = fmt.Sprintf("Document %d: Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam.", i)
	}
	reqBody, _ := json.Marshal(EmbeddingRequest{Input: texts})

	// HTTP client with aggressive connection pooling
	client := &http.Client{
		Timeout: 30 * time.Second,
		Transport: &http.Transport{
			MaxIdleConns:        200,
			MaxIdleConnsPerHost: 200,
			MaxConnsPerHost:     200,
			IdleConnTimeout:     90 * time.Second,
		},
	}

	// Warmup
	fmt.Println("=== Warmup ===")
	resp, err := doRequest(client, url, apiKey, reqBody)
	if err != nil {
		fmt.Printf("ERROR: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("  OK, tokens/batch: %d, dims: %d\n", resp.Usage.TotalTokens, len(resp.Data[0].Embedding))
	tokensPerBatch := resp.Usage.TotalTokens

	// Config
	concurrency := 100
	duration := 60 * time.Second
	targetTPM := 7_000_000

	fmt.Printf("\n=== Config ===\n")
	fmt.Printf("  Concurrency: %d\n", concurrency)
	fmt.Printf("  Duration: %v\n", duration)
	fmt.Printf("  Tokens/batch: %d\n", tokensPerBatch)
	fmt.Printf("  Target: %dM TPM\n", targetTPM/1_000_000)
	fmt.Printf("  Batches needed/sec: %.1f\n", float64(targetTPM)/60/float64(tokensPerBatch))

	// Sustained test
	fmt.Printf("\n=== Sustained throughput test (%v) ===\n", duration)

	var totalTokens atomic.Int64
	var totalBatches atomic.Int64
	var totalErrors atomic.Int64
	var errorCounts sync.Map

	start := time.Now()
	var wg sync.WaitGroup

	// Launch goroutines
	for i := 0; i < concurrency; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for time.Since(start) < duration {
				resp, err := doRequest(client, url, apiKey, reqBody)
				if err != nil {
					totalErrors.Add(1)
					totalBatches.Add(1)
					key := fmt.Sprintf("%v", err)
					if len(key) > 50 {
						key = key[:50]
					}
					val, _ := errorCounts.LoadOrStore(key, new(atomic.Int64))
					val.(*atomic.Int64).Add(1)
					continue
				}
				totalTokens.Add(int64(resp.Usage.TotalTokens))
				totalBatches.Add(1)
			}
		}()
	}

	// Reporter
	go func() {
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			elapsed := time.Since(start).Seconds()
			if elapsed > duration.Seconds()+5 {
				return
			}
			tokens := totalTokens.Load()
			batches := totalBatches.Load()
			errors := totalErrors.Load()
			tpm := float64(tokens) / elapsed * 60
			fmt.Printf("  [%5.0fs] %d batches, %d tokens, %.0f TPM (%.1fM), errors=%d\n",
				elapsed, batches, tokens, tpm, tpm/1_000_000, errors)
		}
	}()

	wg.Wait()
	elapsed := time.Since(start).Seconds()

	tokens := totalTokens.Load()
	batches := totalBatches.Load()
	errors := totalErrors.Load()
	tpm := float64(tokens) / elapsed * 60
	tps := float64(tokens) / elapsed

	fmt.Printf("\n=== Results ===\n")
	fmt.Printf("  Duration:    %.1fs\n", elapsed)
	fmt.Printf("  Batches:     %d (%.1f/sec)\n", batches, float64(batches)/elapsed)
	fmt.Printf("  Tokens:      %d\n", tokens)
	fmt.Printf("  TPM:         %.0f (%.1fM tokens/min)\n", tpm, tpm/1_000_000)
	fmt.Printf("  TPS:         %.0f tokens/sec\n", tps)
	errPct := float64(0)
	if batches > 0 {
		errPct = float64(errors) / float64(batches) * 100
	}
	fmt.Printf("  Errors:      %d (%.1f%%)\n", errors, errPct)
	errorCounts.Range(func(key, val any) bool {
		fmt.Printf("    %s: %d\n", key, val.(*atomic.Int64).Load())
		return true
	})
	fmt.Printf("  Target:      %dM TPM\n", targetTPM/1_000_000)
	fmt.Printf("  Achieved:    %.1f%% of target\n", tpm/float64(targetTPM)*100)
}

func doRequest(client *http.Client, url, apiKey string, body []byte) (*EmbeddingResponse, error) {
	req, _ := http.NewRequest("POST", url, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("api-key", apiKey)

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("HTTP %d: %.200s", resp.StatusCode, string(data))
	}

	var result EmbeddingResponse
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, err
	}
	return &result, nil
}
