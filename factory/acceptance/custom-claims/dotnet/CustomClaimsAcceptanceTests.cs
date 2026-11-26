// Black-box acceptance tests for custom token-request fields (CustomClaims).
// Copied into an SDK checkout by run.sh. Contract: ../contract.md.
//
// The option is set by reflection so that this file compiles against an SDK
// without it; cases that need the option then fail with "option missing".

using OpenFga.Sdk.Client;
using OpenFga.Sdk.Client.Model;
using OpenFga.Sdk.Configuration;
using OpenFga.Sdk.Exceptions;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Xunit;

namespace OpenFga.Sdk.Test.FactoryAcceptance {
    public class CustomClaimsAcceptanceTests {
        private const string Option = "CustomClaims";
        private const string Token = "factory-synthetic-oauth-token";
        private const string StoreId = "01ARZ3NDEKTSV4RRFFQ69G5FAV";
        private const string ModelId = "01ARZ3NDEKTSV4RRFFQ69G5FAW";
        private const string Org = "factory team/one & two";

        private sealed class Case {
            public Dictionary<string, string>? Claims;
            public string? Scopes;
            public bool Audience = true;
            public Dictionary<string, string> Extra = new Dictionary<string, string>();
        }

        private static Dictionary<string, string> Custom() =>
            new Dictionary<string, string> { ["organization"] = Org, ["scope"] = "read:tuples" };

        private static Dictionary<string, string> Reserved() =>
            new Dictionary<string, string> {
                ["organization"] = Org,
                ["client_id"] = "wrong",
                ["client_secret"] = "wrong",
                ["grant_type"] = "wrong",
                ["audience"] = "wrong",
            };

        private static Case CaseFor(string id) {
            switch (id) {
                case "default":
                case "denied":
                    return new Case();
                case "empty":
                    return new Case { Claims = new Dictionary<string, string>() };
                case "custom":
                    return new Case { Claims = Custom(), Extra = Custom() };
                case "reserved":
                    return new Case { Claims = Reserved(), Extra = new Dictionary<string, string> { ["organization"] = Org } };
                case "scopes":
                    return new Case {
                        Claims = Custom(),
                        Scopes = "write:tuples list:objects",
                        Extra = new Dictionary<string, string> { ["organization"] = Org, ["scope"] = "write:tuples list:objects" },
                    };
                case "empty-scopes":
                    return new Case { Claims = Custom(), Scopes = "", Extra = Custom() };
                case "no-audience":
                    return new Case {
                        Claims = Reserved(),
                        Audience = false,
                        Extra = new Dictionary<string, string> { ["organization"] = Org },
                    };
                default:
                    throw new ArgumentException(id);
            }
        }

        private sealed class Fixture : IDisposable {
            private readonly HttpListener _listener = new HttpListener();
            private readonly Task _loop;
            private readonly bool _deny;
            public readonly object Gate = new object();
            public readonly List<(Dictionary<string, string> Form, string ContentType)> Tokens =
                new List<(Dictionary<string, string>, string)>();
            public readonly List<(string User, bool BearerValid)> Apis = new List<(string, bool)>();
            public readonly List<string> Problems = new List<string>();
            public readonly string Url;

            public Fixture(bool deny) {
                _deny = deny;
                var probe = new TcpListener(IPAddress.Loopback, 0);
                probe.Start();
                var port = ((IPEndPoint)probe.LocalEndpoint).Port;
                probe.Stop();
                Url = $"http://127.0.0.1:{port}";
                _listener.Prefixes.Add(Url + "/");
                _listener.Start();
                _loop = Task.Run(Loop);
            }

            private async Task Loop() {
                while (_listener.IsListening) {
                    HttpListenerContext context;
                    try {
                        context = await _listener.GetContextAsync().ConfigureAwait(false);
                    }
                    catch (Exception) {
                        return;
                    }
                    try {
                        Handle(context);
                    }
                    catch (Exception e) {
                        lock (Gate) {
                            Problems.Add("fixture error: " + e.Message);
                        }
                        try {
                            context.Response.Abort();
                        }
                        catch (Exception) {
                        }
                    }
                }
            }

            private void Handle(HttpListenerContext context) {
                var request = context.Request;
                string body;
                using (var reader = new StreamReader(request.InputStream, Encoding.UTF8)) {
                    body = reader.ReadToEnd();
                }
                var path = request.Url!.AbsolutePath;
                var post = request.HttpMethod == "POST";
                lock (Gate) {
                    if (post && path == "/oauth/token") {
                        var form = new Dictionary<string, string>();
                        foreach (var pair in body.Split(new[] { '&' }, StringSplitOptions.RemoveEmptyEntries)) {
                            var eq = pair.IndexOf('=');
                            var key = WebUtility.UrlDecode(eq < 0 ? pair : pair.Substring(0, eq));
                            var value = eq < 0 ? "" : WebUtility.UrlDecode(pair.Substring(eq + 1));
                            if (form.ContainsKey(key)) {
                                Problems.Add("duplicate token form key " + key);
                            }
                            form[key] = value;
                        }
                        Tokens.Add((form, request.ContentType ?? ""));
                        if (_deny) {
                            Reply(context, 401, "{\"error\":\"invalid_client\"}");
                        }
                        else {
                            Reply(context, 200,
                                "{\"access_token\":\"" + Token + "\",\"expires_in\":3600,\"token_type\":\"Bearer\"}");
                        }
                    }
                    else if (post && path == "/stores/" + StoreId + "/check") {
                        string user;
                        using (var doc = JsonDocument.Parse(body)) {
                            user = doc.RootElement.GetProperty("tuple_key").GetProperty("user").GetString() ?? "";
                        }
                        var valid = request.Headers["Authorization"] == "Bearer " + Token;
                        Apis.Add((user, valid));
                        if (!valid) {
                            Reply(context, 401, "{\"code\":\"unauthenticated\"}");
                        }
                        else {
                            Reply(context, 200, "{\"allowed\":" + (user == "user:anne" ? "true" : "false") + "}");
                        }
                    }
                    else {
                        Problems.Add("unexpected route " + request.HttpMethod + " " + path);
                        Reply(context, 404, "{\"code\":\"not_found\"}");
                    }
                }
            }

            private static void Reply(HttpListenerContext context, int status, string json) {
                var bytes = Encoding.UTF8.GetBytes(json);
                context.Response.StatusCode = status;
                context.Response.ContentType = "application/json";
                context.Response.ContentLength64 = bytes.Length;
                context.Response.OutputStream.Write(bytes, 0, bytes.Length);
                context.Response.OutputStream.Close();
            }

            public void Dispose() {
                _listener.Stop();
                _listener.Close();
                try {
                    _loop.Wait(TimeSpan.FromSeconds(5));
                }
                catch (Exception) {
                }
            }
        }

        private static ICredentialsConfig WithCustomClaims(CredentialsConfig config, Dictionary<string, string>? claims) {
            if (claims == null) {
                return config;
            }
            var property = typeof(CredentialsConfig).GetProperty(Option);
            if (property == null || !property.CanWrite ||
                !property.PropertyType.IsAssignableFrom(typeof(Dictionary<string, string>))) {
                Assert.Fail("option missing: CredentialsConfig." + Option + " (IDictionary<string, string>)");
            }
            object boxed = config;
            property!.SetValue(boxed, claims);
            return (ICredentialsConfig)boxed;
        }

        private static int? StatusOf(Exception? error) {
            var pending = new Stack<Exception>();
            if (error != null) {
                pending.Push(error);
            }
            while (pending.Count > 0) {
                var e = pending.Pop();
                if (e is FgaApiError api) {
                    return (int)api.StatusCode;
                }
                if (e is AggregateException aggregate) {
                    foreach (var inner in aggregate.InnerExceptions) {
                        pending.Push(inner);
                    }
                }
                else if (e.InnerException != null) {
                    pending.Push(e.InnerException);
                }
            }
            return null;
        }

        private static Dictionary<string, string> ExpectedForm(Case c) {
            var form = new Dictionary<string, string> {
                ["client_id"] = "factory-client",
                ["client_secret"] = "factory-secret",
                ["grant_type"] = "client_credentials",
            };
            if (c.Audience) {
                form["audience"] = "factory-audience";
            }
            if (!string.IsNullOrWhiteSpace(c.Scopes)) {
                form["scope"] = c.Scopes!;
            }
            foreach (var pair in c.Extra) {
                form[pair.Key] = pair.Value;
            }
            return form;
        }

        private static string Describe(Dictionary<string, string> form) =>
            string.Join("&", form.OrderBy(p => p.Key, StringComparer.Ordinal).Select(p => p.Key + "=" + p.Value));

        private static async Task<bool> Check(OpenFgaClient client, string user) {
            var response = await client.Check(new ClientCheckRequest {
                User = user,
                Relation = "viewer",
                Object = "document:factory",
            });
            return response.Allowed == true;
        }

        [Theory]
        [InlineData("default")]
        [InlineData("empty")]
        [InlineData("custom")]
        [InlineData("reserved")]
        [InlineData("denied")]
        [InlineData("scopes")]
        [InlineData("empty-scopes")]
        [InlineData("no-audience")]
        public async Task CustomClaims(string id) {
            var c = CaseFor(id);
            var denied = id == "denied";
            var snapshot = c.Claims == null ? null : new Dictionary<string, string>(c.Claims);

            using var fixture = new Fixture(denied);
            var credentialsConfig = new CredentialsConfig {
                ClientId = "factory-client",
                ClientSecret = "factory-secret",
                ApiTokenIssuer = fixture.Url + "/oauth/token",
                ApiAudience = c.Audience ? "factory-audience" : null,
                Scopes = c.Scopes,
            };
            var configuration = new ClientConfiguration {
                ApiUrl = fixture.Url,
                StoreId = StoreId,
                AuthorizationModelId = ModelId,
                Credentials = new Credentials {
                    Method = CredentialsMethod.ClientCredentials,
                    Config = WithCustomClaims(credentialsConfig, c.Claims),
                },
                MaxRetry = 0,
            };
            using var client = new OpenFgaClient(configuration);

            if (denied) {
                Exception? error = null;
                try {
                    await Check(client, "user:anne");
                }
                catch (Exception e) {
                    error = e;
                }
                Assert.True(error != null, "denied: expected a token error");
                Assert.Equal(401, StatusOf(error));
            }
            else {
                Assert.True(await Check(client, "user:anne"), "user:anne must be allowed");
                Assert.False(await Check(client, "user:bob"), "user:bob must be denied");
            }

            lock (fixture.Gate) {
                Assert.Empty(fixture.Problems);
                if (snapshot != null) {
                    Assert.Equal(Describe(snapshot), Describe(c.Claims!));
                }
                if (denied) {
                    Assert.Empty(fixture.Apis);
                    Assert.NotEmpty(fixture.Tokens);
                }
                else {
                    Assert.Equal(new[] { ("user:anne", true), ("user:bob", true) }, fixture.Apis.ToArray());
                    Assert.Single(fixture.Tokens);
                }
                foreach (var token in fixture.Tokens) {
                    Assert.StartsWith("application/x-www-form-urlencoded", token.ContentType);
                    Assert.Equal(Describe(ExpectedForm(c)), Describe(token.Form));
                }
            }
        }
    }
}
