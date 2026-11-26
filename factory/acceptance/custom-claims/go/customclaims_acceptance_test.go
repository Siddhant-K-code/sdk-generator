// Black-box acceptance tests for custom token-request fields (customClaims).
// Copied into an SDK checkout by run.sh. Contract: ../contract.md.
//
// The option is set by reflection so that this file compiles against an SDK
// without it; cases that need the option then fail with "option missing".
package customclaims_test

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"sync"
	"testing"

	openfga "github.com/openfga/go-sdk"
	"github.com/openfga/go-sdk/client"
	"github.com/openfga/go-sdk/credentials"
	"github.com/openfga/go-sdk/oauth2"
)

const (
	optionName   = "ClientCredentialsCustomClaims"
	token        = "factory-synthetic-oauth-token"
	storeID      = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
	modelID      = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
	clientID     = "factory-client"
	clientSecret = "factory-secret"
	org          = "factory team/one & two"
)

type tokenCase struct {
	id       string
	claims   map[string]string // nil = option unset
	scopes   string
	audience bool
	extra    map[string]string // expected fields beyond the native baseline
}

func reserved() map[string]string {
	return map[string]string{
		"organization": org, "client_id": "wrong", "client_secret": "wrong",
		"grant_type": "wrong", "audience": "wrong",
	}
}

func custom() map[string]string {
	return map[string]string{"organization": org, "scope": "read:tuples"}
}

func cases() []tokenCase {
	return []tokenCase{
		{id: "default", audience: true, extra: map[string]string{}},
		{id: "empty", claims: map[string]string{}, audience: true, extra: map[string]string{}},
		{id: "custom", claims: custom(), audience: true, extra: map[string]string{"organization": org, "scope": "read:tuples"}},
		{id: "reserved", claims: reserved(), audience: true, extra: map[string]string{"organization": org}},
		{id: "denied", audience: true, extra: map[string]string{}},
		{id: "scopes", claims: custom(), scopes: "write:tuples list:objects", audience: true, extra: map[string]string{"organization": org, "scope": "write:tuples list:objects"}},
		{id: "empty-scopes", claims: custom(), scopes: "", audience: true, extra: map[string]string{"organization": org, "scope": "read:tuples"}},
		{id: "no-audience", claims: reserved(), audience: false, extra: map[string]string{"organization": org}},
	}
}

type tokenRequest struct {
	form        map[string]string
	basicUser   string
	basicPass   string
	hasBasic    bool
	contentType string
}

type apiRequest struct {
	user        string
	bearerValid bool
}

type fixture struct {
	mu       sync.Mutex
	deny     bool
	tokens   []tokenRequest
	apis     []apiRequest
	problems []string
}

func (f *fixture) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	f.mu.Lock()
	defer f.mu.Unlock()
	reply := func(status int, v any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(v)
	}
	switch {
	case r.Method == http.MethodPost && r.URL.Path == "/oauth/token":
		values, err := url.ParseQuery(string(body))
		if err != nil {
			f.problems = append(f.problems, "unparseable token form: "+err.Error())
		}
		form := map[string]string{}
		for k, v := range values {
			if len(v) != 1 {
				f.problems = append(f.problems, "duplicate token form key "+k)
			}
			form[k] = v[0]
		}
		user, pass, ok := r.BasicAuth()
		f.tokens = append(f.tokens, tokenRequest{form, user, pass, ok, r.Header.Get("Content-Type")})
		if f.deny {
			reply(http.StatusUnauthorized, map[string]string{"error": "invalid_client"})
			return
		}
		reply(http.StatusOK, map[string]any{"access_token": token, "expires_in": 3600, "token_type": "Bearer"})
	case r.Method == http.MethodPost && r.URL.Path == "/stores/"+storeID+"/check":
		var req struct {
			TupleKey struct {
				User string `json:"user"`
			} `json:"tuple_key"`
		}
		_ = json.Unmarshal(body, &req)
		valid := r.Header.Get("Authorization") == "Bearer "+token
		f.apis = append(f.apis, apiRequest{req.TupleKey.User, valid})
		if !valid {
			reply(http.StatusUnauthorized, map[string]string{"code": "unauthenticated"})
			return
		}
		reply(http.StatusOK, map[string]bool{"allowed": req.TupleKey.User == "user:anne"})
	default:
		f.problems = append(f.problems, "unexpected route "+r.Method+" "+r.URL.Path)
		reply(http.StatusNotFound, map[string]string{"code": "not_found"})
	}
}

func setCustomClaims(t *testing.T, cfg *credentials.Config, claims map[string]string) {
	t.Helper()
	field := reflect.ValueOf(cfg).Elem().FieldByName(optionName)
	if !field.IsValid() {
		t.Fatalf("option missing: credentials.Config.%s", optionName)
	}
	value := reflect.ValueOf(claims)
	if !value.Type().AssignableTo(field.Type()) {
		t.Fatalf("option credentials.Config.%s has type %s, want map[string]string", optionName, field.Type())
	}
	field.Set(value)
}

func statusOf(err error) int {
	var coded interface{ ResponseStatusCode() int }
	if errors.As(err, &coded) {
		return coded.ResponseStatusCode()
	}
	var retrieve *oauth2.RetrieveError
	if errors.As(err, &retrieve) && retrieve.Response != nil {
		return retrieve.Response.StatusCode
	}
	return 0
}

func check(fga *client.OpenFgaClient, user string) (bool, error) {
	resp, err := fga.Check(context.Background()).Body(client.ClientCheckRequest{
		User: user, Relation: "viewer", Object: "document:factory",
	}).Execute()
	if err != nil {
		return false, err
	}
	return resp.GetAllowed(), nil
}

func expectedForm(tc tokenCase) map[string]string {
	form := map[string]string{"grant_type": "client_credentials"}
	if tc.audience {
		form["audience"] = "factory-audience"
	}
	if strings.TrimSpace(tc.scopes) != "" {
		form["scope"] = tc.scopes
	}
	maps.Copy(form, tc.extra)
	return form
}

func TestCustomClaims(t *testing.T) {
	for _, tc := range cases() {
		t.Run(tc.id, func(t *testing.T) {
			denied := tc.id == "denied"
			fx := &fixture{deny: denied}
			srv := httptest.NewServer(fx)
			defer srv.Close()

			cfg := &credentials.Config{
				ClientCredentialsClientId:       clientID,
				ClientCredentialsClientSecret:   clientSecret,
				ClientCredentialsApiTokenIssuer: srv.URL + "/oauth/token",
				ClientCredentialsScopes:         tc.scopes,
			}
			if tc.audience {
				cfg.ClientCredentialsApiAudience = "factory-audience"
			}
			snapshot := maps.Clone(tc.claims)
			if tc.claims != nil {
				setCustomClaims(t, cfg, tc.claims)
			}

			fga, err := client.NewSdkClient(&client.ClientConfiguration{
				ApiUrl:               srv.URL,
				StoreId:              storeID,
				AuthorizationModelId: modelID,
				Credentials:          &credentials.Credentials{Method: credentials.CredentialsMethodClientCredentials, Config: cfg},
				RetryParams:          &openfga.RetryParams{MaxRetry: 0, MinWaitInMs: 10},
			})
			if err != nil {
				t.Fatalf("NewSdkClient: %v", err)
			}

			allowed, err := check(fga, "user:anne")
			if denied {
				if err == nil {
					t.Fatalf("denied: expected token error, got allowed=%v", allowed)
				}
				if got := statusOf(err); got != http.StatusUnauthorized {
					t.Fatalf("denied: expected status 401, got %d (%v)", got, err)
				}
			} else {
				if err != nil || !allowed {
					t.Fatalf("user:anne: want allowed=true, got %v err=%v", allowed, err)
				}
				allowed, err = check(fga, "user:bob")
				if err != nil || allowed {
					t.Fatalf("user:bob: want allowed=false, got %v err=%v", allowed, err)
				}
			}

			fx.mu.Lock()
			defer fx.mu.Unlock()
			if len(fx.problems) > 0 {
				t.Fatalf("fixture problems: %v", fx.problems)
			}
			if !maps.Equal(tc.claims, snapshot) {
				t.Errorf("caller's custom claims map was mutated: %v, want %v", tc.claims, snapshot)
			}
			if denied {
				if len(fx.apis) != 0 {
					t.Errorf("denied: want 0 API requests, got %v", fx.apis)
				}
				if len(fx.tokens) < 1 {
					t.Errorf("denied: want at least one token request")
				}
			} else {
				want := []apiRequest{{"user:anne", true}, {"user:bob", true}}
				if !reflect.DeepEqual(fx.apis, want) {
					t.Errorf("API requests: got %v, want %v", fx.apis, want)
				}
				if len(fx.tokens) != 1 {
					t.Errorf("want exactly one token exchange (cached), got %d", len(fx.tokens))
				}
			}

			for i, req := range fx.tokens {
				if !strings.HasPrefix(req.contentType, "application/x-www-form-urlencoded") {
					t.Errorf("token request %d: content type %q", i, req.contentType)
				}
				form := maps.Clone(req.form)
				id, hasID := form["client_id"]
				secret, hasSecret := form["client_secret"]
				delete(form, "client_id")
				delete(form, "client_secret")
				if (hasID && id != clientID) || (hasSecret && secret != clientSecret) {
					t.Errorf("token request %d: form credentials overridden: %v", i, req.form)
				}
				if req.hasBasic && (req.basicUser != clientID || req.basicPass != clientSecret) {
					t.Errorf("token request %d: basic credentials overridden: %q", i, req.basicUser)
				}
				if !req.hasBasic && !(hasID && hasSecret) {
					t.Errorf("token request %d: no client credentials sent", i)
				}
				if want := expectedForm(tc); !maps.Equal(form, want) {
					t.Errorf("token request %d: form %v, want %v (+ client credentials)", i, form, want)
				}
			}
		})
	}
}
