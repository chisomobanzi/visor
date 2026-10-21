// Minimal JSON value, parser and writer for the control protocol.
//
// The protocol is small (one object per line), so this favours being short and
// obviously correct over speed. Objects keep insertion order so replies read
// the way they were built.
#pragma once

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace visor {

class Json
{
public:
	enum class Type { Null, Bool, Number, String, Array, Object };

	Json() = default;
	Json(std::nullptr_t) {}
	Json(bool b) : type_(Type::Bool), b_(b) {}
	Json(int v) : type_(Type::Number), n_(v) {}
	Json(unsigned v) : type_(Type::Number), n_(v) {}
	Json(long v) : type_(Type::Number), n_((double)v) {}
	Json(unsigned long v) : type_(Type::Number), n_((double)v) {}
	Json(long long v) : type_(Type::Number), n_((double)v) {}
	Json(unsigned long long v) : type_(Type::Number), n_((double)v) {}
	Json(float v) : type_(Type::Number), n_(v) {}
	Json(double v) : type_(Type::Number), n_(v) {}
	Json(const char* s) : type_(Type::String), s_(s ? s : "") {}
	Json(std::string s) : type_(Type::String), s_(std::move(s)) {}

	static Json array() { Json j; j.type_ = Type::Array; return j; }
	static Json object() { Json j; j.type_ = Type::Object; return j; }

	Type type() const { return type_; }
	bool is_null() const { return type_ == Type::Null; }
	bool is_bool() const { return type_ == Type::Bool; }
	bool is_number() const { return type_ == Type::Number; }
	bool is_string() const { return type_ == Type::String; }
	bool is_array() const { return type_ == Type::Array; }
	bool is_object() const { return type_ == Type::Object; }

	// Lenient readers: return the fallback when the type doesn't match.
	bool as_bool(bool fallback = false) const;
	double as_number(double fallback = 0.0) const;
	int64_t as_int(int64_t fallback = 0) const { return is_number() ? (int64_t)n_ : fallback; }
	const std::string& as_string() const { return s_; }
	std::string as_string(const std::string& fallback) const { return is_string() ? s_ : fallback; }

	// Arrays.
	size_t size() const { return is_array() ? a_.size() : (is_object() ? o_.size() : 0); }
	const Json& at(size_t i) const;
	void push(Json v);
	const std::vector<Json>& items() const { return a_; }

	// Objects. get() returns nullptr when missing; operator[] inserts.
	const Json* get(const std::string& key) const;
	bool has(const std::string& key) const { return get(key) != nullptr; }
	Json& operator[](const std::string& key);
	const std::vector<std::pair<std::string, Json>>& entries() const { return o_; }

	static Json parse(const std::string& text, std::string* error = nullptr);
	std::string dump() const;

private:
	void dump_to(std::string& out) const;

	Type type_ = Type::Null;
	bool b_ = false;
	double n_ = 0.0;
	std::string s_;
	std::vector<Json> a_;
	std::vector<std::pair<std::string, Json>> o_;
};

} // namespace visor
