#pragma once

#include <util/memory/alloc.h>
#include <util/digest/numeric.h>
#include <util/generic/string.h>
#include <util/generic/strbuf.h>
#include <util/generic/typetraits.h>

#include <utility>
#include <functional>

namespace std {
    template <>
    struct less<const char*>: public std::function<bool(const char*, const char*)> {
        bool operator()(const char* x, const char* y) const {
            return strcmp(x, y) < 0;
        }
    };
    template <>
    struct equal_to<const char*>: public std::function<bool(const char*, const char*)> {
        bool operator()(const char* x, const char* y) const {
            return strcmp(x, y) == 0;
        }
        bool operator()(const char* x, const TFixedString<char> y) const {
            return strlen(x) == y.Length && memcmp(x, y.Start, y.Length) == 0;
        }
        using is_transparent = void;
    };
}

namespace NHashPrivate {
    template <class T, bool needNumericHashing>
    struct THashHelper {
        inline size_t operator()(const T& t) const noexcept {
            return (size_t)t; // If you have a compilation error here, look at explanation below:
            // Probably error is caused by undefined template specialization of THash<T>
            // You can find examples of specialization in this file
        }
    };

    template <class T>
    struct THashHelper<T, true> {
        inline size_t operator()(const T& t) const noexcept {
            return NumericHash(t);
        }
    };
}

template <class T>
struct hash: public NHashPrivate::THashHelper<T, std::is_scalar<T>::value && !std::is_integral<T>::value> {
};

template <typename T>
struct hash<const T*> {
    inline size_t operator()(const T* t) const noexcept {
        return NumericHash(t);
    }
};

template <class T>
struct hash<T*>: public ::hash<const T*> {
};

template <>
struct hash<const char*> {
    // note that const char* is implicitly converted to TFixedString
    inline size_t operator()(const TFixedString<char> s) const noexcept {
        return TString::hashVal(s.Start, s.Length);
    }
};

template <>
struct hash<TString> {
    inline size_t operator()(const TStringBuf s) const {
        return s.hash();
    }
};

template <>
struct hash<TUtf16String> {
    inline size_t operator()(const TWtringBuf s) const {
        return s.hash();
    }
};

template <>
struct hash<TUtf32String> {
    inline size_t operator()(const TUtf32StringBuf s) const {
        return s.hash();
    }
};


template <class C, class T, class A>
struct hash<std::basic_string<C, T, A>> {
    inline size_t operator()(const TStringBufImpl<C>& s) const {
        return s.hash();
    }
};

namespace NHashPrivate {
    template <typename T>
    Y_FORCE_INLINE static size_t HashObject(const T& val) {
        return THash<T>()(val);
    }

    template <size_t I, bool IsLastElement, typename... TArgs>
    struct TupleHashHelper {
        Y_FORCE_INLINE static size_t Hash(const std::tuple<TArgs...>& tuple) {
            return CombineHashes(HashObject(std::get<I>(tuple)),
                                 TupleHashHelper<I + 1, I + 2 >= sizeof...(TArgs), TArgs...>::Hash(tuple));
        }
    };

    template <size_t I, typename... TArgs>
    struct TupleHashHelper<I, true, TArgs...> {
        Y_FORCE_INLINE static size_t Hash(const std::tuple<TArgs...>& tuple) {
            return HashObject(std::get<I>(tuple));
        }
    };

}

template <typename... TArgs>
struct THash<std::tuple<TArgs...>> {
    size_t operator()(const std::tuple<TArgs...>& tuple) const {
        return NHashPrivate::TupleHashHelper<0, 1 >= sizeof...(TArgs), TArgs...>::Hash(tuple);
    }
};

template <class T>
struct THash: public ::hash<T> {
};

namespace NHashPrivate {
    template <class TFirst, class TSecond, bool IsEmpty = std::is_empty<THash<TFirst>>::value&& std::is_empty<THash<TSecond>>::value>
    struct TPairHash {
    private:
        THash<TFirst> FirstHash;
        THash<TSecond> SecondHash;

    public:
        template <class T>
        inline size_t operator()(const T& pair) const {
            return CombineHashes(FirstHash(pair.first), SecondHash(pair.second));
        }
    };

    /**
     * Specialization for the case where both hash functors are empty. Basically the
     * only one we care about. We don't introduce additional specializations for
     * cases where only one of the functors is empty as the code bloat is just not worth it.
     */
    template <class TFirst, class TSecond>
    struct TPairHash<TFirst, TSecond, true> {
        template <class T>
        inline size_t operator()(const T& pair) const {
            return CombineHashes(THash<TFirst>()(pair.first), THash<TSecond>()(pair.second));
        }
    };
}

template <class TFirst, class TSecond>
struct hash<std::pair<TFirst, TSecond>>: public NHashPrivate::TPairHash<TFirst, TSecond> {
};

template <>
struct THash<TStringBuf> {
    inline size_t operator()(const TStringBuf s) const {
        return s.hash();
    }
};

template <>
struct THash<TWtringBuf> {
    inline size_t operator()(const TWtringBuf s) const {
        return s.hash();
    }
};

template <>
struct THash<TUtf32StringBuf> {
    inline size_t operator()(const TUtf32StringBuf s) const {
        return s.hash();
    }
};


template <class T>
struct TEqualTo: public std::equal_to<T> {
};

template <>
struct TEqualTo<TString>: public TEqualTo<TStringBuf> {
    using is_transparent = void;
};

template <>
struct TEqualTo<TUtf16String>: public TEqualTo<TWtringBuf> {
    using is_transparent = void;
};

template <>
struct TEqualTo<TUtf32String> : public TEqualTo<TUtf32StringBuf> {
    using is_transparent = void;
};

template <class TFirst, class TSecond>
struct TEqualTo<std::pair<TFirst, TSecond>> {
    template <class TOther>
    inline bool operator()(const std::pair<TFirst, TSecond>& a, const TOther& b) const {
        return TEqualTo<TFirst>()(a.first, b.first) && TEqualTo<TSecond>()(a.second, b.second);
    }
    using is_transparent = void;
};

template <class T>
struct TCIEqualTo {
};

template <>
struct TCIEqualTo<const char*> {
    inline bool operator()(const char* a, const char* b) const {
        return stricmp(a, b) == 0;
    }
};

template <>
struct TCIEqualTo<TStringBuf> {
    inline bool operator()(const TStringBuf a, const TStringBuf b) const {
        return a.size() == b.size() && strnicmp(a.data(), b.data(), a.size()) == 0;
    }
};

template <>
struct TCIEqualTo<TString> {
    inline bool operator()(const TString& a, const TString& b) const {
        return a.size() == b.size() && strnicmp(a.data(), b.data(), a.size()) == 0;
    }
};

template <class T>
struct TLess: public std::less<T> {
};

template <>
struct TLess<TString>: public TLess<TStringBuf> {
    using is_transparent = void;
};

template <>
struct TLess<TUtf16String>: public TLess<TWtringBuf> {
    using is_transparent = void;
};

template <>
struct TLess<TUtf32String> : public TLess<TUtf32StringBuf> {
    using is_transparent = void;
};

template <class T>
struct TGreater: public std::greater<T> {
};

template <>
struct TGreater<TString>: public TGreater<TStringBuf> {
    using is_transparent = void;
};

template <>
struct TGreater<TUtf16String>: public TGreater<TWtringBuf> {
    using is_transparent = void;
};

template <>
struct TGreater<TUtf32String> : public TGreater<TUtf32StringBuf> {
    using is_transparent = void;
};
