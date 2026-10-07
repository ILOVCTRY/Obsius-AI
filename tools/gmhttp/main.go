// gmhttp —— 国密 TLS / 标准 TLS 单次 HTTP 请求 sidecar（cyberstrike-pro 重发国密通道）。
//
// 背景（2026-10-07）：Python 的 ssl 是 OpenSSL 薄绑定，本机 OpenSSL 未编入 SM 密码套件，
// 且 Python 未暴露 set_ciphersuites；PyPI 上的 gmssl/gmalg/pygmssl 等只有 SM2/SM3/SM4
// 密码学原语、没有 TLS 栈。Go 生态有成熟国密 TLS 实现（tjfoc/gmsm 的 gmtls，fork 自
// crypto/tls 并补上 GM/T 0024 套件），故国密传输走本 sidecar，其余走 Python httpx。
//
// 协议（一次性进程，纯 Go 零 cgo）：
//   stdin  ← 一个 JSON 请求规格 Spec
//   stdout → 一个 JSON 结果 Result（二进制 body 走 base64）
// 规格解析失败 → stderr 输出原因 + exit 2；请求失败 → 正常输出 Result{error:...} + exit 0
// （让 Python 侧统一按结果对象处理，与 httpx 分支「连接失败也入库」语义一致）。
package main

import (
	"context"
	"crypto/tls"
	stdx509 "crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/tjfoc/gmsm/gmtls"
	gmx509 "github.com/tjfoc/gmsm/x509"
)

// Spec 是 Python 侧传入的请求规格。
type Spec struct {
	Method          string            `json:"method"`
	URL             string            `json:"url"`
	Headers         map[string]string `json:"headers"`
	BodyB64         string            `json:"body_b64"`
	GM              bool              `json:"gm"`               // true=国密 TLS（GM/T 0024）
	FollowRedirects bool              `json:"follow_redirects"` // false=不跟随（返回 3xx 原响应）
	Insecure        bool              `json:"insecure"`         // true=跳过证书校验
	Proxy           string            `json:"proxy"`            // 显式代理；空=直连（绝不读系统代理）
	TimeoutMS       int               `json:"timeout_ms"`
	BodyMaxBytes    int               `json:"body_max_bytes"`
	ServerName      string            `json:"server_name"`    // SNI 覆写（留空取 URL host）
	ClientCertB64   string            `json:"client_cert_b64"` // 可选客户端证书 PEM（国密双证书：两块 CERTIFICATE）
	ClientKeyB64    string            `json:"client_key_b64"`  // 可选客户端私钥 PEM（与证书块数一一对应）
	CACertB64       string            `json:"ca_cert_b64"`     // 可选自定义 CA PEM
}

// Result 是回传 Python 的结果对象。
type Result struct {
	Status        *int              `json:"status"`
	Headers       map[string]string `json:"headers"`
	BodyB64       string            `json:"body_b64"`
	Mime          string            `json:"mime"`
	DurationMS    int64             `json:"duration_ms"`
	BodyTruncated bool              `json:"body_truncated"`
	Error         string            `json:"error"`
}

const (
	defaultTimeout = 15 * time.Second
	defaultBodyMax = 65536
)

func main() {
	// gmtls 库会把「handshake error : ...」直接打到 os.Stdout，污染 JSON 协议流
	// （Python 侧按整段 stdout 解析）。把 os.Stdout 换成 stderr，真实 stdout 只留给结果。
	realOut := os.Stdout
	os.Stdout = os.Stderr
	var s Spec
	if err := json.NewDecoder(os.Stdin).Decode(&s); err != nil {
		fmt.Fprintf(os.Stderr, "bad spec: %v\n", err)
		os.Exit(2)
	}
	enc := json.NewEncoder(realOut)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(doRequest(s)); err != nil {
		fmt.Fprintf(os.Stderr, "encode result: %v\n", err)
		os.Exit(2)
	}
}

func timeoutOf(s Spec) time.Duration {
	if s.TimeoutMS > 0 {
		return time.Duration(s.TimeoutMS) * time.Millisecond
	}
	return defaultTimeout
}

func serverNameOf(s Spec) string {
	if s.ServerName != "" {
		return s.ServerName
	}
	u, err := url.Parse(s.URL)
	if err != nil {
		return ""
	}
	return u.Hostname()
}

// splitPEM 按块类型拆 PEM（证书块 / 私钥块），保留原编码便于回填。
func splitPEM(data []byte, wantCert bool) [][]byte {
	var out [][]byte
	rest := data
	for {
		var b *pem.Block
		b, rest = pem.Decode(rest)
		if b == nil {
			break
		}
		isCert := b.Type == "CERTIFICATE"
		if isCert == wantCert {
			out = append(out, pem.EncodeToMemory(b))
		}
	}
	return out
}

func decodeB64(field, val string) ([]byte, error) {
	raw, err := base64.StdEncoding.DecodeString(val)
	if err != nil {
		return nil, fmt.Errorf("%s base64 解码失败: %w", field, err)
	}
	return raw, nil
}

// buildStdConfig 标准 TLS（crypto/tls + crypto/x509）。
func buildStdConfig(s Spec) (*tls.Config, error) {
	cfg := &tls.Config{
		ServerName:         serverNameOf(s),
		InsecureSkipVerify: s.Insecure,
		MinVersion:         tls.VersionTLS12,
	}
	if s.CACertB64 != "" {
		raw, err := decodeB64("CA 证书", s.CACertB64)
		if err != nil {
			return nil, err
		}
		pool := stdx509.NewCertPool()
		if !pool.AppendCertsFromPEM(raw) {
			return nil, fmt.Errorf("CA 证书 PEM 解析失败")
		}
		cfg.RootCAs = pool
	}
	if s.ClientCertB64 != "" && s.ClientKeyB64 != "" {
		certRaw, err := decodeB64("客户端证书", s.ClientCertB64)
		if err != nil {
			return nil, err
		}
		keyRaw, err := decodeB64("客户端私钥", s.ClientKeyB64)
		if err != nil {
			return nil, err
		}
		pair, err := tls.X509KeyPair(certRaw, keyRaw)
		if err != nil {
			return nil, fmt.Errorf("客户端证书/私钥解析失败: %w", err)
		}
		cfg.Certificates = []tls.Certificate{pair}
	}
	return cfg, nil
}

// buildGMConfig 国密 TLS（gmtls + gmsm/x509）。
// 客户端证书块数与私钥块数 1:1 配对：一块=普通客户端证书，两块=国密双证书（签名+加密）。
func buildGMConfig(s Spec) (*gmtls.Config, error) {
	cfg := &gmtls.Config{
		GMSupport:          &gmtls.GMSupport{WorkMode: gmtls.ModeGMSSLOnly},
		ServerName:         serverNameOf(s),
		InsecureSkipVerify: s.Insecure,
		MinVersion:         gmtls.VersionGMSSL,
		MaxVersion:         gmtls.VersionGMSSL,
	}
	if s.CACertB64 != "" {
		raw, err := decodeB64("CA 证书", s.CACertB64)
		if err != nil {
			return nil, err
		}
		pool := gmx509.NewCertPool()
		if !pool.AppendCertsFromPEM(raw) {
			return nil, fmt.Errorf("CA 证书 PEM 解析失败")
		}
		cfg.RootCAs = pool
	}
	if s.ClientCertB64 != "" && s.ClientKeyB64 != "" {
		certRaw, err := decodeB64("客户端证书", s.ClientCertB64)
		if err != nil {
			return nil, err
		}
		keyRaw, err := decodeB64("客户端私钥", s.ClientKeyB64)
		if err != nil {
			return nil, err
		}
		certBlocks := splitPEM(certRaw, true)
		keyBlocks := splitPEM(keyRaw, false)
		if len(certBlocks) == 0 || len(certBlocks) != len(keyBlocks) {
			return nil, fmt.Errorf("国密客户端证书/私钥数量不匹配：%d 证书 vs %d 私钥",
				len(certBlocks), len(keyBlocks))
		}
		pairs := make([]gmtls.Certificate, 0, len(certBlocks))
		for i := range certBlocks {
			c, err := gmtls.X509KeyPair(certBlocks[i], keyBlocks[i])
			if err != nil {
				return nil, fmt.Errorf("国密客户端证书解析失败: %w", err)
			}
			pairs = append(pairs, c)
		}
		cfg.Certificates = pairs
	}
	return cfg, nil
}

func buildTransport(s Spec) (*http.Transport, error) {
	tr := &http.Transport{}
	// 显式代理才设；否则 Proxy 恒 nil —— 与 httpx trust_env=False 同款红线：
	// 重发打授权目标（常为内网/localhost），流量绝不交系统代理（防 Clash 等劫持）。
	if s.Proxy != "" {
		u, err := url.Parse(s.Proxy)
		if err != nil {
			return nil, fmt.Errorf("非法代理地址: %w", err)
		}
		tr.Proxy = http.ProxyURL(u)
	}
	if s.GM {
		cfg, err := buildGMConfig(s)
		if err != nil {
			return nil, err
		}
		tr.DialTLSContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
			d := &net.Dialer{Timeout: timeoutOf(s)}
			if deadline, ok := ctx.Deadline(); ok {
				d.Deadline = deadline
			}
			return gmtls.DialWithDialer(d, network, addr, cfg)
		}
	} else {
		cfg, err := buildStdConfig(s)
		if err != nil {
			return nil, err
		}
		tr.TLSClientConfig = cfg
	}
	return tr, nil
}

func doRequest(s Spec) Result {
	start := time.Now()
	tr, err := buildTransport(s)
	if err != nil {
		return Result{Headers: map[string]string{}, Error: err.Error()}
	}
	client := &http.Client{
		Transport: tr,
		Timeout:   timeoutOf(s),
		CheckRedirect: func(*http.Request, []*http.Request) error {
			if s.FollowRedirects {
				return nil
			}
			return http.ErrUseLastResponse // 不跟随：把 3xx 原样返回
		},
	}
	var bodyReader io.Reader
	if s.BodyB64 != "" {
		raw, err := decodeB64("请求体", s.BodyB64)
		if err != nil {
			return Result{Headers: map[string]string{}, Error: err.Error()}
		}
		bodyReader = strings.NewReader(string(raw))
	}
	req, err := http.NewRequest(strings.ToUpper(s.Method), s.URL, bodyReader)
	if err != nil {
		return Result{Headers: map[string]string{}, Error: "构造请求失败: " + err.Error()}
	}
	for k, v := range s.Headers {
		req.Header.Set(k, v)
	}
	// 与 httpx 一致：不自动补 User-Agent（Go 默认会加 Go-http-client/1.1，显式置空规避）
	if _, ok := s.Headers["User-Agent"]; !ok {
		req.Header.Set("User-Agent", "")
	}
	resp, err := client.Do(req)
	if err != nil {
		return Result{Headers: map[string]string{},
			DurationMS: time.Since(start).Milliseconds(),
			Error:      err.Error()}
	}
	defer resp.Body.Close()

	max := s.BodyMaxBytes
	if max <= 0 {
		max = defaultBodyMax
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, int64(max)+1))
	if err != nil {
		return Result{Status: &resp.StatusCode, Headers: flattenHeaders(resp.Header),
			DurationMS: time.Since(start).Milliseconds(),
			Error:      "读取响应体失败: " + err.Error()}
	}
	truncated := len(data) > max
	if truncated {
		data = data[:max]
	}
	return Result{
		Status:        &resp.StatusCode,
		Headers:       flattenHeaders(resp.Header),
		BodyB64:       base64.StdEncoding.EncodeToString(data),
		Mime:          resp.Header.Get("Content-Type"),
		DurationMS:    time.Since(start).Milliseconds(),
		BodyTruncated: truncated,
	}
}

// flattenHeaders 把多值头合并为 ", " 连接（与 Python dict(httpx.Headers) 口径一致）。
func flattenHeaders(h http.Header) map[string]string {
	out := make(map[string]string, len(h))
	for k, vs := range h {
		out[k] = strings.Join(vs, ", ")
	}
	return out
}
