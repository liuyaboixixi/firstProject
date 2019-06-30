package com.javaproject.demo.mapper;


import com.javaproject.demo.model.Question;

public interface QuestionExtMapper {
    int incView(Question record);
    int incCommentCount(Question record);
}