package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type Client struct {
	BaseURL    string
	Token      string
	HTTPClient *http.Client
}

func NewClient(baseURL, token string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		Token:   token,
		HTTPClient: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

func (c *Client) Get(path string, params map[string]string) (json.RawMessage, error) {
	u := c.BaseURL + path
	if len(params) > 0 {
		q := url.Values{}
		for k, v := range params {
			if v != "" {
				q.Set(k, v)
			}
		}
		if encoded := q.Encode(); encoded != "" {
			u += "?" + encoded
		}
	}
	return c.do("GET", u, nil)
}

func (c *Client) Post(path string, body any) (json.RawMessage, error) {
	u := c.BaseURL + path
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("marshal request: %w", err)
		}
		reader = bytes.NewReader(data)
	}
	return c.do("POST", u, reader)
}

func (c *Client) Put(path string, body any) (json.RawMessage, error) {
	u := c.BaseURL + path
	data, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}
	return c.do("PUT", u, bytes.NewReader(data))
}

func (c *Client) Delete(path string) (json.RawMessage, error) {
	u := c.BaseURL + path
	return c.do("DELETE", u, nil)
}

func (c *Client) do(method, url string, body io.Reader) (json.RawMessage, error) {
	req, err := http.NewRequest(method, url, body)
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("cannot connect to server at %s: %v", c.BaseURL, err)
	}
	defer resp.Body.Close()

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode >= 400 {
		// Try to extract detail from JSON error response
		var errResp map[string]any
		if json.Unmarshal(data, &errResp) == nil {
			if detail, ok := errResp["detail"]; ok {
				return nil, fmt.Errorf("[%d] %v", resp.StatusCode, detail)
			}
		}
		return nil, fmt.Errorf("[%d] %s", resp.StatusCode, strings.TrimSpace(string(data)))
	}

	return json.RawMessage(data), nil
}
