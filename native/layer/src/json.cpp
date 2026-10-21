#include "json.h"

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace visor {

static const Json kNull;

bool Json::as_bool(bool fallback) const
{
	if (is_bool()) {
		return b_;
	}
	if (is_number()) {
		return n_ != 0.0;
	}
	return fallback;
}

double Json::as_number(double fallback) const
{
	if (is_number()) {
		return n_;
	}
	if (is_bool()) {
		return b_ ? 1.0 : 0.0;
	}
	return fallback;
}

const Json& Json::at(size_t i) const
{
	if (!is_array() || i >= a_.size()) {
		return kNull;
	}
	return a_[i];
}

void Json::push(Json v)
{
	if (!is_array()) {
		*this = array();
	}
	a_.push_back(std::move(v));
}

const Json* Json::get(const std::string& key) const
{
	if (!is_object()) {
		return nullptr;
	}
	for (const auto& kv : o_) {
		if (kv.first == key) {
			return &kv.second;
		}
	}
	return nullptr;
}

Json& Json::operator[](const std::string& key)
{
	if (!is_object()) {
		*this = object();
	}
	for (auto& kv : o_) {
		if (kv.first == key) {
			return kv.second;
		}
	}
	o_.emplace_back(key, Json());
	return o_.back().second;
}

// ---------------------------------------------------------------------------
// Parser

namespace {

struct Parser
{
	const std::string& s;
	size_t i = 0;
	std::string err;

	explicit Parser(const std::string& text) : s(text) {}

	void ws()
	{
		while (i < s.size() && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) {
			i++;
		}
	}

	bool fail(const char* what)
	{
		if (err.empty()) {
			err = std::string(what) + " at offset " + std::to_string(i);
		}
		return false;
	}

	bool literal(const char* word)
	{
		size_t n = std::strlen(word);
		if (s.compare(i, n, word) != 0) {
			return fail("unexpected token");
		}
		i += n;
		return true;
	}

	static void utf8(std::string& out, uint32_t cp)
	{
		if (cp < 0x80) {
			out += (char)cp;
		} else if (cp < 0x800) {
			out += (char)(0xC0 | (cp >> 6));
			out += (char)(0x80 | (cp & 0x3F));
		} else if (cp < 0x10000) {
			out += (char)(0xE0 | (cp >> 12));
			out += (char)(0x80 | ((cp >> 6) & 0x3F));
			out += (char)(0x80 | (cp & 0x3F));
		} else {
			out += (char)(0xF0 | (cp >> 18));
			out += (char)(0x80 | ((cp >> 12) & 0x3F));
			out += (char)(0x80 | ((cp >> 6) & 0x3F));
			out += (char)(0x80 | (cp & 0x3F));
		}
	}

	bool hex4(uint32_t& out)
	{
		if (i + 4 > s.size()) {
			return fail("short unicode escape");
		}
		out = 0;
		for (int k = 0; k < 4; k++) {
			char c = s[i++];
			out <<= 4;
			if (c >= '0' && c <= '9') {
				out |= (uint32_t)(c - '0');
			} else if (c >= 'a' && c <= 'f') {
				out |= (uint32_t)(c - 'a' + 10);
			} else if (c >= 'A' && c <= 'F') {
				out |= (uint32_t)(c - 'A' + 10);
			} else {
				return fail("bad unicode escape");
			}
		}
		return true;
	}

	bool string(std::string& out)
	{
		if (s[i] != '"') {
			return fail("expected string");
		}
		i++;
		while (i < s.size()) {
			char c = s[i++];
			if (c == '"') {
				return true;
			}
			if (c != '\\') {
				out += c;
				continue;
			}
			if (i >= s.size()) {
				break;
			}
			char e = s[i++];
			switch (e) {
			case '"': out += '"'; break;
			case '\\': out += '\\'; break;
			case '/': out += '/'; break;
			case 'b': out += '\b'; break;
			case 'f': out += '\f'; break;
			case 'n': out += '\n'; break;
			case 'r': out += '\r'; break;
			case 't': out += '\t'; break;
			case 'u': {
				uint32_t cp = 0;
				if (!hex4(cp)) {
					return false;
				}
				if (cp >= 0xD800 && cp <= 0xDBFF && i + 6 <= s.size() && s[i] == '\\' && s[i + 1] == 'u') {
					i += 2;
					uint32_t lo = 0;
					if (!hex4(lo)) {
						return false;
					}
					cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
				}
				utf8(out, cp);
				break;
			}
			default: return fail("bad escape");
			}
		}
		return fail("unterminated string");
	}

	bool value(Json& out, int depth)
	{
		if (depth > 64) {
			return fail("nesting too deep");
		}
		ws();
		if (i >= s.size()) {
			return fail("unexpected end");
		}
		char c = s[i];
		if (c == '{') {
			i++;
			out = Json::object();
			ws();
			if (i < s.size() && s[i] == '}') {
				i++;
				return true;
			}
			while (true) {
				ws();
				std::string key;
				if (i >= s.size() || !string(key)) {
					return fail("expected key");
				}
				ws();
				if (i >= s.size() || s[i] != ':') {
					return fail("expected ':'");
				}
				i++;
				Json v;
				if (!value(v, depth + 1)) {
					return false;
				}
				out[key] = std::move(v);
				ws();
				if (i < s.size() && s[i] == ',') {
					i++;
					continue;
				}
				if (i < s.size() && s[i] == '}') {
					i++;
					return true;
				}
				return fail("expected ',' or '}'");
			}
		}
		if (c == '[') {
			i++;
			out = Json::array();
			ws();
			if (i < s.size() && s[i] == ']') {
				i++;
				return true;
			}
			while (true) {
				Json v;
				if (!value(v, depth + 1)) {
					return false;
				}
				out.push(std::move(v));
				ws();
				if (i < s.size() && s[i] == ',') {
					i++;
					continue;
				}
				if (i < s.size() && s[i] == ']') {
					i++;
					return true;
				}
				return fail("expected ',' or ']'");
			}
		}
		if (c == '"') {
			std::string str;
			if (!string(str)) {
				return false;
			}
			out = Json(std::move(str));
			return true;
		}
		if (c == 't') {
			out = Json(true);
			return literal("true");
		}
		if (c == 'f') {
			out = Json(false);
			return literal("false");
		}
		if (c == 'n') {
			out = Json();
			return literal("null");
		}
		// Number.
		const char* start = s.c_str() + i;
		char* end = nullptr;
		double d = std::strtod(start, &end);
		if (end == start) {
			return fail("unexpected character");
		}
		i += (size_t)(end - start);
		out = Json(d);
		return true;
	}
};

} // namespace

Json Json::parse(const std::string& text, std::string* error)
{
	Parser p(text);
	Json out;
	if (!p.value(out, 0)) {
		if (error) {
			*error = p.err;
		}
		return Json();
	}
	p.ws();
	if (p.i != text.size()) {
		if (error) {
			*error = "trailing characters at offset " + std::to_string(p.i);
		}
		return Json();
	}
	return out;
}

// ---------------------------------------------------------------------------
// Writer

static void escape(std::string& out, const std::string& s)
{
	out += '"';
	for (unsigned char c : s) {
		switch (c) {
		case '"': out += "\\\""; break;
		case '\\': out += "\\\\"; break;
		case '\n': out += "\\n"; break;
		case '\r': out += "\\r"; break;
		case '\t': out += "\\t"; break;
		default:
			if (c < 0x20) {
				char buf[8];
				std::snprintf(buf, sizeof(buf), "\\u%04x", c);
				out += buf;
			} else {
				out += (char)c;
			}
		}
	}
	out += '"';
}

void Json::dump_to(std::string& out) const
{
	switch (type_) {
	case Type::Null: out += "null"; break;
	case Type::Bool: out += b_ ? "true" : "false"; break;
	case Type::Number: {
		if (!std::isfinite(n_)) {
			out += "null";
		} else if (n_ == std::floor(n_) && std::fabs(n_) < 9.0e15) {
			char buf[32];
			std::snprintf(buf, sizeof(buf), "%lld", (long long)n_);
			out += buf;
		} else {
			char buf[32];
			std::snprintf(buf, sizeof(buf), "%.6g", n_);
			out += buf;
		}
		break;
	}
	case Type::String: escape(out, s_); break;
	case Type::Array:
		out += '[';
		for (size_t k = 0; k < a_.size(); k++) {
			if (k) {
				out += ',';
			}
			a_[k].dump_to(out);
		}
		out += ']';
		break;
	case Type::Object:
		out += '{';
		for (size_t k = 0; k < o_.size(); k++) {
			if (k) {
				out += ',';
			}
			escape(out, o_[k].first);
			out += ':';
			o_[k].second.dump_to(out);
		}
		out += '}';
		break;
	}
}

std::string Json::dump() const
{
	std::string out;
	dump_to(out);
	return out;
}

} // namespace visor
